# -*- coding: utf-8 -*-
# Copyright 2019 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import re
from typing import Callable, Dict, Optional, Set, Tuple

import attr
import saml2
import saml2.response
from saml2.client import Saml2Client

from synapse.api.errors import SynapseError
from synapse.config import ConfigError
from synapse.http.servlet import parse_string
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from synapse.types import (
    UserID,
    map_username_to_mxid_localpart,
    mxid_localpart_allowed_characters,
)
from synapse.util.async_helpers import Linearizer
from synapse.util.iterutils import chunk_seq

logger = logging.getLogger(__name__)


@attr.s
class Saml2SessionData:
    """Data we track about SAML2 sessions"""

    # time the session was created, in milliseconds
    creation_time = attr.ib()
    # The user interactive authentication session ID associated with this SAML
    # session (or None if this SAML session is for an initial login).
    ui_auth_session_id = attr.ib(type=Optional[str], default=None)


class SamlHandler:
    def __init__(self, hs):
        self._saml_client = Saml2Client(hs.config.saml2_sp_config)
        self._auth = hs.get_auth()
        self._auth_handler = hs.get_auth_handler()
        self._registration_handler = hs.get_registration_handler()

        self._clock = hs.get_clock()
        self._datastore = hs.get_datastore()
        self._hostname = hs.hostname
        self._saml2_session_lifetime = hs.config.saml2_session_lifetime
        self._grandfathered_mxid_source_attribute = (
            hs.config.saml2_grandfathered_mxid_source_attribute
        )

        # plugin to do custom mapping from saml response to mxid
        self._user_mapping_provider = hs.config.saml2_user_mapping_provider_class(
            hs.config.saml2_user_mapping_provider_config,
            ModuleApi(hs, hs.get_auth_handler()),
        )

        # identifier for the external_ids table
        self._auth_provider_id = "saml"

        # a map from saml session id to Saml2SessionData object
        self._outstanding_requests_dict = {}

        # a lock on the mappings
        self._mapping_lock = Linearizer(name="saml_mapping", clock=self._clock)

    def handle_redirect_request(
        self, client_redirect_url: bytes, ui_auth_session_id: Optional[str] = None
    ) -> bytes:
        """Handle an incoming request to /login/sso/redirect

        Args:
            client_redirect_url: the URL that we should redirect the
                client to when everything is done
            ui_auth_session_id: The session ID of the ongoing UI Auth (or
                None if this is a login).

        Returns:
            URL to redirect to
        """
        reqid, info = self._saml_client.prepare_for_authenticate(
            relay_state=client_redirect_url
        )

        now = self._clock.time_msec()
        self._outstanding_requests_dict[reqid] = Saml2SessionData(
            creation_time=now, ui_auth_session_id=ui_auth_session_id,
        )

        for key, value in info["headers"]:
            if key == "Location":
                return value

        # this shouldn't happen!
        raise Exception("prepare_for_authenticate didn't return a Location header")

    async def handle_saml_response(self, request: SynapseRequest) -> None:
        """Handle an incoming request to /_matrix/saml2/authn_response

        Args:
            request: the incoming request from the browser. We'll
                respond to it with a redirect.

        Returns:
            Completes once we have handled the request.
        """
        resp_bytes = parse_string(request, "SAMLResponse", required=True)
        relay_state = parse_string(request, "RelayState", required=True)

        # expire outstanding sessions before parse_authn_request_response checks
        # the dict.
        self.expire_sessions()

        user_id, current_session = await self._map_saml_response_to_user(
            resp_bytes, relay_state
        )

        # Complete the interactive auth session or the login.
        if current_session and current_session.ui_auth_session_id:
            await self._auth_handler.complete_sso_ui_auth(
                user_id, current_session.ui_auth_session_id, request
            )

        else:
            await self._auth_handler.complete_sso_login(user_id, request, relay_state)

    async def _map_saml_response_to_user(
        self, resp_bytes: str, client_redirect_url: str
    ) -> Tuple[str, Optional[Saml2SessionData]]:
        """
        Given a sample response, retrieve the cached session and user for it.

        Args:
            resp_bytes: The SAML response.
            client_redirect_url: The redirect URL passed in by the client.

        Returns:
             Tuple of the user ID and SAML session associated with this response.

        Raises:
            SynapseError if there was a problem with the response.
            RedirectException: some mapping providers may raise this if they need
                to redirect to an interstitial page.
        """
        try:
            saml2_auth = self._saml_client.parse_authn_request_response(
                resp_bytes,
                saml2.BINDING_HTTP_POST,
                outstanding=self._outstanding_requests_dict,
            )
        except Exception as e:
            raise SynapseError(400, "Unable to parse SAML2 response: %s" % (e,))

        if saml2_auth.not_signed:
            raise SynapseError(400, "SAML2 response was not signed")

        logger.debug("SAML2 response: %s", saml2_auth.origxml)
        for assertion in saml2_auth.assertions:
            # kibana limits the length of a log field, whereas this is all rather
            # useful, so split it up.
            count = 0
            for part in chunk_seq(str(assertion), 10000):
                logger.info(
                    "SAML2 assertion: %s%s", "(%i)..." % (count,) if count else "", part
                )
                count += 1

        logger.info("SAML2 mapped attributes: %s", saml2_auth.ava)

        current_session = self._outstanding_requests_dict.pop(
            saml2_auth.in_response_to, None
        )

        remote_user_id = self._user_mapping_provider.get_remote_user_id(
            saml2_auth, client_redirect_url
        )

        if not remote_user_id:
            raise Exception("Failed to extract remote user id from SAML response")

        with (await self._mapping_lock.queue(self._auth_provider_id)):
            # first of all, check if we already have a mapping for this user
            logger.info(
                "Looking for existing mapping for user %s:%s",
                self._auth_provider_id,
                remote_user_id,
            )
            registered_user_id = await self._datastore.get_user_by_external_id(
                self._auth_provider_id, remote_user_id
            )
            if registered_user_id is not None:
                logger.info("Found existing mapping %s", registered_user_id)
                return registered_user_id, current_session

            # backwards-compatibility hack: see if there is an existing user with a
            # suitable mapping from the uid
            if (
                self._grandfathered_mxid_source_attribute
                and self._grandfathered_mxid_source_attribute in saml2_auth.ava
            ):
                attrval = saml2_auth.ava[self._grandfathered_mxid_source_attribute][0]
                user_id = UserID(
                    map_username_to_mxid_localpart(attrval), self._hostname
                ).to_string()
                logger.info(
                    "Looking for existing account based on mapped %s %s",
                    self._grandfathered_mxid_source_attribute,
                    user_id,
                )

                users = await self._datastore.get_users_by_id_case_insensitive(user_id)
                if users:
                    registered_user_id = list(users.keys())[0]
                    logger.info("Grandfathering mapping to %s", registered_user_id)
                    await self._datastore.record_user_external_id(
                        self._auth_provider_id, remote_user_id, registered_user_id
                    )
                    return registered_user_id, current_session

            # Map saml response to user attributes using the configured mapping provider
            for i in range(1000):
                attribute_dict = self._user_mapping_provider.saml_response_to_user_attributes(
                    saml2_auth, i, client_redirect_url=client_redirect_url,
                )

                logger.debug(
                    "Retrieved SAML attributes from user mapping provider: %s "
                    "(attempt %d)",
                    attribute_dict,
                    i,
                )

                localpart = attribute_dict.get("mxid_localpart")
                if not localpart:
                    raise Exception(
                        "Error parsing SAML2 response: SAML mapping provider plugin "
                        "did not return a mxid_localpart value"
                    )

                displayname = attribute_dict.get("displayname")
                emails = attribute_dict.get("emails", [])

                # Check if this mxid already exists
                if not await self._datastore.get_users_by_id_case_insensitive(
                    UserID(localpart, self._hostname).to_string()
                ):
                    # This mxid is free
                    break
            else:
                # Unable to generate a username in 1000 iterations
                # Break and return error to the user
                raise SynapseError(
                    500, "Unable to generate a Matrix ID from the SAML response"
                )

            logger.info("Mapped SAML user to local part %s", localpart)

            registered_user_id = await self._registration_handler.register_user(
                localpart=localpart,
                default_display_name=displayname,
                bind_emails=emails,
            )

            await self._datastore.record_user_external_id(
                self._auth_provider_id, remote_user_id, registered_user_id
            )
            return registered_user_id, current_session

    def expire_sessions(self):
        expire_before = self._clock.time_msec() - self._saml2_session_lifetime
        to_expire = set()
        for reqid, data in self._outstanding_requests_dict.items():
            if data.creation_time < expire_before:
                to_expire.add(reqid)
        for reqid in to_expire:
            logger.debug("Expiring session id %s", reqid)
            del self._outstanding_requests_dict[reqid]


DOT_REPLACE_PATTERN = re.compile(
    ("[^%s]" % (re.escape("".join(mxid_localpart_allowed_characters)),))
)


def dot_replace_for_mxid(username: str) -> str:
    """Replace any characters which are not allowed in Matrix IDs with a dot."""
    username = username.lower()
    username = DOT_REPLACE_PATTERN.sub(".", username)

    # regular mxids aren't allowed to start with an underscore either
    username = re.sub("^_", "", username)
    return username


MXID_MAPPER_MAP = {
    "hexencode": map_username_to_mxid_localpart,
    "dotreplace": dot_replace_for_mxid,
}  # type: Dict[str, Callable[[str], str]]


@attr.s
class SamlConfig(object):
    mxid_source_attribute = attr.ib()
    mxid_mapper = attr.ib()


class DefaultSamlMappingProvider(object):
    __version__ = "0.0.1"

    def __init__(self, parsed_config: SamlConfig, module_api: ModuleApi):
        """The default SAML user mapping provider

        Args:
            parsed_config: Module configuration
            module_api: module api proxy
        """
        self._mxid_source_attribute = parsed_config.mxid_source_attribute
        self._mxid_mapper = parsed_config.mxid_mapper

        self._grandfathered_mxid_source_attribute = (
            module_api._hs.config.saml2_grandfathered_mxid_source_attribute
        )

    def get_remote_user_id(
        self, saml_response: saml2.response.AuthnResponse, client_redirect_url: str
    ) -> str:
        """Extracts the remote user id from the SAML response"""
        try:
            return saml_response.ava["uid"][0]
        except KeyError:
            logger.warning("SAML2 response lacks a 'uid' attestation")
            raise SynapseError(400, "'uid' not in SAML2 response")

    def saml_response_to_user_attributes(
        self,
        saml_response: saml2.response.AuthnResponse,
        failures: int,
        client_redirect_url: str,
    ) -> dict:
        """Maps some text from a SAML response to attributes of a new user

        Args:
            saml_response: A SAML auth response object

            failures: How many times a call to this function with this
                saml_response has resulted in a failure

            client_redirect_url: where the client wants to redirect to

        Returns:
            dict: A dict containing new user attributes. Possible keys:
                * mxid_localpart (str): Required. The localpart of the user's mxid
                * displayname (str): The displayname of the user
                * emails (list[str]): Any emails for the user
        """
        try:
            mxid_source = saml_response.ava[self._mxid_source_attribute][0]
        except KeyError:
            logger.warning(
                "SAML2 response lacks a '%s' attestation", self._mxid_source_attribute,
            )
            raise SynapseError(
                400, "%s not in SAML2 response" % (self._mxid_source_attribute,)
            )

        # Use the configured mapper for this mxid_source
        base_mxid_localpart = self._mxid_mapper(mxid_source)

        # Append suffix integer if last call to this function failed to produce
        # a usable mxid
        localpart = base_mxid_localpart + (str(failures) if failures else "")

        # Retrieve the display name from the saml response
        # If displayname is None, the mxid_localpart will be used instead
        displayname = saml_response.ava.get("displayName", [None])[0]

        # Retrieve any emails present in the saml response
        emails = saml_response.ava.get("email", [])

        return {
            "mxid_localpart": localpart,
            "displayname": displayname,
            "emails": emails,
        }

    @staticmethod
    def parse_config(config: dict) -> SamlConfig:
        """Parse the dict provided by the homeserver's config
        Args:
            config: A dictionary containing configuration options for this provider
        Returns:
            SamlConfig: A custom config object for this module
        """
        # Parse config options and use defaults where necessary
        mxid_source_attribute = config.get("mxid_source_attribute", "uid")
        mapping_type = config.get("mxid_mapping", "hexencode")

        # Retrieve the associating mapping function
        try:
            mxid_mapper = MXID_MAPPER_MAP[mapping_type]
        except KeyError:
            raise ConfigError(
                "saml2_config.user_mapping_provider.config: '%s' is not a valid "
                "mxid_mapping value" % (mapping_type,)
            )

        return SamlConfig(mxid_source_attribute, mxid_mapper)

    @staticmethod
    def get_saml_attributes(config: SamlConfig) -> Tuple[Set[str], Set[str]]:
        """Returns the required attributes of a SAML

        Args:
            config: A SamlConfig object containing configuration params for this provider

        Returns:
            The first set equates to the saml auth response
                attributes that are required for the module to function, whereas the
                second set consists of those attributes which can be used if
                available, but are not necessary
        """
        return {"uid", config.mxid_source_attribute}, {"displayName", "email"}
