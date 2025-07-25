"""
This file defines a post load hook for dynaconf,
After loading all the settings files from all enabled Pulp plugins and envvars,
dynaconf will call a function named `post` and if that function returns a
dictionary containing {key:value} those values will be added, or merged to
the previously loaded settings.

This file exists to enable conditionally loaded settings variables, variables
that depends on other variable state and then requires the final state of the
settings before making conditionals.

Read more: https://www.dynaconf.com/advanced/#hooks
"""
import json
import logging
import os
import re
import sys
from typing import Any

import ldap
from ansible_base.lib.dynamic_config import (
    factory,
    load_dab_settings,
    load_standard_settings_files,
    toggle_feature_flags,
    validate as dab_validate,
)
from crum import get_current_request
from django.apps import apps
from django_auth_ldap.config import LDAPSearch
from dynaconf import Dynaconf, Validator

from galaxy_ng.app.dynamic_settings import DYNAMIC_SETTINGS_SCHEMA

if sys.version_info < (3, 10):
    # Python 3.9 has a rather different interface for `entry_points`.
    # Let's use a compatibility version.
    from importlib_metadata import EntryPoint
else:
    from importlib.metadata import EntryPoint

logger = logging.getLogger(__name__)

DAB_SERVICE_BACKED_REDIRECT = (
    "ansible_base.resource_registry.utils.service_backed_sso_pipeline.redirect_to_resource_server"
)


def post(settings: Dynaconf, run_dynamic: bool = True, run_validate: bool = True) -> dict[str, Any]:
    """The dynaconf post hook is called after all the settings are loaded and set.

    Post hook is necessary when a setting key depends conditionally on a previouslys et variable.

    settings: A read-only copy of the django.conf.settings
    run_dynamic: update the final data with configure_dynamic_settings
    run_validate: call the validate function on the final data
    returns: a dictionary to be merged to django.conf.settings

    NOTES:
        Feature flags must be loaded directly on `app/api/ui/views/feature_flags.py` view.
    """

    data = {"dynaconf_merge": False}
    # existing keys will be merged if dynaconf_merge is set to True
    # here it is set to false, so it allows each value to be individually marked as a merge.

    data.update(configure_ldap(settings))
    data.update(configure_logging(settings))
    data.update(configure_keycloak(settings))
    data.update(configure_socialauth(settings))
    data.update(configure_cors(settings))
    data.update(configure_pulp_ansible(settings))
    data.update(configure_renderers(settings))
    data.update(configure_password_validators(settings))
    data.update(configure_api_base_path(settings))
    data.update(configure_legacy_roles(settings))
    # these must get the current state of data to make decisions
    data.update(configure_dab_required_settings(settings, data))
    data.update(toggle_feature_flags(settings))

    # These should go last, and it needs to receive the data from the previous configuration
    # functions because this function configures the rest framework auth classes based off
    # of the galaxy auth classes, and if galaxy auth classes are overridden by any of the
    # other dynaconf hooks (such as keycloak), those changes need to be applied to the
    # rest framework auth classes too.
    data.update(configure_authentication_backends(settings, data))
    data.update(configure_authentication_classes(settings, data))

    # When the resource server is configured, local resource management is disabled.
    data["IS_CONNECTED_TO_RESOURCE_SERVER"] = settings.get("RESOURCE_SERVER__URL") is not None

    # This must go last, so that all the default settings are loaded before dynamic and validation
    if run_dynamic:
        data.update(configure_dynamic_settings(settings))

    if run_validate:
        validate(settings)

    # must go right before returning the data
    data.update(configure_dynaconf_cli(settings, data))

    return data


def configure_keycloak(settings: Dynaconf) -> dict[str, Any]:
    """Configure keycloak settings for galaxy.

    This function returns a dictionary that will be merged to the settings.
    """

    data = {}

    # Obtain values for Social Auth
    SOCIAL_AUTH_KEYCLOAK_KEY = settings.get("SOCIAL_AUTH_KEYCLOAK_KEY", default=None)
    SOCIAL_AUTH_KEYCLOAK_SECRET = settings.get("SOCIAL_AUTH_KEYCLOAK_SECRET", default=None)
    SOCIAL_AUTH_KEYCLOAK_PUBLIC_KEY = settings.get("SOCIAL_AUTH_KEYCLOAK_PUBLIC_KEY", default=None)
    KEYCLOAK_PROTOCOL = settings.get("KEYCLOAK_PROTOCOL", default=None)
    KEYCLOAK_HOST = settings.get("KEYCLOAK_HOST", default=None)
    KEYCLOAK_PORT = settings.get("KEYCLOAK_PORT", default=None)
    KEYCLOAK_REALM = settings.get("KEYCLOAK_REALM", default=None)

    # Add settings if Social Auth values are provided
    if all(
        [
            SOCIAL_AUTH_KEYCLOAK_KEY,
            SOCIAL_AUTH_KEYCLOAK_SECRET,
            SOCIAL_AUTH_KEYCLOAK_PUBLIC_KEY,
            KEYCLOAK_HOST,
            KEYCLOAK_PORT,
            KEYCLOAK_REALM,
        ]
    ):

        data["GALAXY_AUTH_KEYCLOAK_ENABLED"] = True

        data["KEYCLOAK_ADMIN_ROLE"] = settings.get("KEYCLOAK_ADMIN_ROLE", default="hubadmin")
        data["KEYCLOAK_GROUP_TOKEN_CLAIM"] = settings.get(
            "KEYCLOAK_GROUP_TOKEN_CLAIM", default="group"
        )
        data["KEYCLOAK_ROLE_TOKEN_CLAIM"] = settings.get(
            "KEYCLOAK_GROUP_TOKEN_CLAIM", default="client_roles"
        )
        data["KEYCLOAK_HOST_LOOPBACK"] = settings.get("KEYCLOAK_HOST_LOOPBACK", default=None)
        data["KEYCLOAK_URL"] = f"{KEYCLOAK_PROTOCOL}://{KEYCLOAK_HOST}:{KEYCLOAK_PORT}"
        auth_url_str = "{keycloak}/auth/realms/{realm}/protocol/openid-connect/auth/"
        data["SOCIAL_AUTH_KEYCLOAK_AUTHORIZATION_URL"] = auth_url_str.format(
            keycloak=data["KEYCLOAK_URL"], realm=KEYCLOAK_REALM
        )
        if data["KEYCLOAK_HOST_LOOPBACK"]:
            loopback_url = "{protocol}://{host}:{port}".format(
                protocol=KEYCLOAK_PROTOCOL, host=data["KEYCLOAK_HOST_LOOPBACK"], port=KEYCLOAK_PORT
            )
            data["SOCIAL_AUTH_KEYCLOAK_AUTHORIZATION_URL"] = auth_url_str.format(
                keycloak=loopback_url, realm=KEYCLOAK_REALM
            )

        data[
            "SOCIAL_AUTH_KEYCLOAK_ACCESS_TOKEN_URL"
        ] = f"{data['KEYCLOAK_URL']}/auth/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token/"

        data["SOCIAL_AUTH_LOGIN_REDIRECT_URL"] = settings.get(
            "SOCIAL_AUTH_LOGIN_REDIRECT_URL", default="/ui/"
        )
        data["SOCIAL_AUTH_JSONFIELD_ENABLED"] = True
        data["SOCIAL_AUTH_URL_NAMESPACE"] = "social"
        data["SOCIAL_AUTH_KEYCLOAK_EXTRA_DATA"] = [
            ("refresh_token", "refresh_token"),
            (data["KEYCLOAK_ROLE_TOKEN_CLAIM"], data["KEYCLOAK_ROLE_TOKEN_CLAIM"]),
        ]

        data["SOCIAL_AUTH_PIPELINE"] = (
            "social_core.pipeline.social_auth.social_details",
            "social_core.pipeline.social_auth.social_uid",
            "social_core.pipeline.social_auth.social_user",
            "social_core.pipeline.user.get_username",
            "social_core.pipeline.social_auth.associate_by_email",
            "social_core.pipeline.user.create_user",
            "social_core.pipeline.social_auth.associate_user",
            "social_core.pipeline.social_auth.load_extra_data",
            "social_core.pipeline.user.user_details",
            "galaxy_ng.app.pipelines.user_role",
            "galaxy_ng.app.pipelines.user_group",
            DAB_SERVICE_BACKED_REDIRECT,
        )

        # Set external authentication feature flag
        # data["GALAXY_FEATURE_FLAGS"] = {'external_authentication': True, "dynaconf_merge": True}
        # The next have the same effect ^
        data["GALAXY_FEATURE_FLAGS__external_authentication"] = True

        # Add to installed apps
        data["INSTALLED_APPS"] = ["automated_logging", "social_django", "dynaconf_merge_unique"]

        # Add to authentication backends
        data["AUTHENTICATION_BACKENDS"] = [
            "social_core.backends.keycloak.KeycloakOAuth2",
            "dynaconf_merge",
        ]

        # Replace AUTH CLASSES [shifted to configure_authentication_classes]

        # Set default to one day expiration
        data["GALAXY_TOKEN_EXPIRATION"] = settings.get("GALAXY_TOKEN_EXPIRATION", 1440)

        # Add to templates
        # Pending dynaconf issue:
        # https://github.com/rochacbruno/dynaconf/issues/299#issuecomment-900616706
        # So we can do a merge of this data.
        data["TEMPLATES"] = [
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "DIRS": [os.path.join(settings.BASE_DIR, "templates")],
                "APP_DIRS": True,
                "OPTIONS": {
                    "context_processors": [
                        # BEGIN: Pulp standard context processors
                        "django.template.context_processors.debug",
                        "django.template.context_processors.request",
                        "django.contrib.auth.context_processors.auth",
                        "django.contrib.messages.context_processors.messages",
                        # END: Pulp standard context processors
                        "social_django.context_processors.backends",
                        "social_django.context_processors.login_redirect",
                    ],
                },
            },
        ]

    return data


def configure_socialauth(settings: Dynaconf) -> dict[str, Any]:
    """Configure social auth settings for galaxy.

    This function returns a dictionary that will be merged to the settings.
    """

    data = {}

    SOCIAL_AUTH_GITHUB_KEY = settings.get("SOCIAL_AUTH_GITHUB_KEY", default=None)
    SOCIAL_AUTH_GITHUB_SECRET = settings.get("SOCIAL_AUTH_GITHUB_SECRET", default=None)

    if all([SOCIAL_AUTH_GITHUB_KEY, SOCIAL_AUTH_GITHUB_SECRET]):

        # Add to installed apps
        data["INSTALLED_APPS"] = ["social_django", "dynaconf_merge_unique"]

        # Make sure the UI knows to do ext auth
        data["GALAXY_FEATURE_FLAGS__external_authentication"] = True

        backends = settings.get("AUTHENTICATION_BACKENDS", default=[])
        backends.append("galaxy_ng.social.GalaxyNGOAuth2")
        backends.append("dynaconf_merge")
        data["AUTHENTICATION_BACKENDS"] = backends
        data["DEFAULT_AUTHENTICATION_BACKENDS"] = backends
        data["GALAXY_AUTHENTICATION_BACKENDS"] = backends

        data['DEFAULT_AUTHENTICATION_CLASSES'] = [
            "galaxy_ng.app.auth.session.SessionAuthentication",
            "rest_framework.authentication.TokenAuthentication",
            "rest_framework.authentication.BasicAuthentication",
        ]

        data['GALAXY_AUTHENTICATION_CLASSES'] = [
            "galaxy_ng.app.auth.session.SessionAuthentication",
            "rest_framework.authentication.TokenAuthentication",
            "rest_framework.authentication.BasicAuthentication",
        ]

        data['REST_FRAMEWORK_AUTHENTICATION_CLASSES'] = [
            "galaxy_ng.app.auth.session.SessionAuthentication",
            "rest_framework.authentication.TokenAuthentication",
            "rest_framework.authentication.BasicAuthentication",
        ]

        # Override the get_username and create_user steps
        # to conform to our super special user validation
        # requirements
        data['SOCIAL_AUTH_PIPELINE'] = [
            'social_core.pipeline.social_auth.social_details',
            'social_core.pipeline.social_auth.social_uid',
            'social_core.pipeline.social_auth.auth_allowed',
            'social_core.pipeline.social_auth.social_user',
            'galaxy_ng.social.pipeline.user.get_username',
            'galaxy_ng.social.pipeline.user.create_user',
            'social_core.pipeline.social_auth.associate_user',
            'social_core.pipeline.social_auth.load_extra_data',
            'social_core.pipeline.user.user_details',
            DAB_SERVICE_BACKED_REDIRECT
        ]

    return data


def configure_logging(settings: Dynaconf) -> dict[str, Any]:
    data = {
        "GALAXY_ENABLE_API_ACCESS_LOG": settings.get(
            "GALAXY_ENABLE_API_ACCESS_LOG",
            default=os.getenv("GALAXY_ENABLE_API_ACCESS_LOG", default=False),
        )
    }
    if data["GALAXY_ENABLE_API_ACCESS_LOG"]:
        data["INSTALLED_APPS"] = ["galaxy_ng._vendor.automated_logging", "dynaconf_merge_unique"]
        data["MIDDLEWARE"] = [
            "automated_logging.middleware.AutomatedLoggingMiddleware",
            "dynaconf_merge",
        ]
        data["LOGGING"] = {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "automated_logging": {"format": "%(asctime)s: %(levelname)s: %(message)s"},
            },
            "handlers": {
                "automated_logging": {
                    "level": "INFO",
                    "class": "logging.handlers.WatchedFileHandler",
                    "filename": "/var/log/galaxy_api_access.log",
                    "formatter": "automated_logging",
                },
            },
            "loggers": {
                "automated_logging": {
                    "handlers": ["automated_logging"],
                    "level": "INFO",
                    "propagate": False,
                },
            },
            "dynaconf_merge": True,
        }
        data["AUTOMATED_LOGGING"] = {
            "globals": {
                "exclude": {
                    "applications": [
                        "plain:contenttypes",
                        "plain:admin",
                        "plain:basehttp",
                        "glob:session*",
                        "plain:migrations",
                    ]
                }
            },
            "model": {
                "detailed_message": True,
                "exclude": {"applications": [], "fields": [], "models": [], "unknown": False},
                "loglevel": 20,
                "mask": [],
                "max_age": None,
                "performance": False,
                "snapshot": False,
                "user_mirror": False,
            },
            "modules": ["request", "unspecified", "model"],
            "request": {
                "data": {
                    "content_types": ["application/json"],
                    "enabled": [],
                    "ignore": [],
                    "mask": [
                        "ca_cert",
                        "client_cert",
                        "client_key",
                        "email",
                        "password",
                        "proxy_url",
                        "proxy_username",
                        "proxy_password",
                        "token",
                        "username",
                    ],
                    "query": True,
                },
                "exclude": {
                    "applications": [],
                    "methods": [],
                    "status": [],
                    "unknown": False,
                },
                "ip": True,
                "loglevel": 20,
                "max_age": None,
            },
            "unspecified": {
                "exclude": {"applications": [], "files": [], "unknown": False},
                "loglevel": 20,
                "max_age": None,
            },
        }

    return data


def configure_cors(settings: Dynaconf) -> dict[str, Any]:
    """This adds CORS Middleware, useful to access swagger UI on dev environment"""

    if os.getenv("DEV_SOURCE_PATH", None) is None:
        # Only add CORS if we are in dev mode
        return {}

    data = {}
    if settings.get("GALAXY_ENABLE_CORS", default=False):
        corsmiddleware = ["galaxy_ng.app.common.openapi.AllowCorsMiddleware"]
        data["MIDDLEWARE"] = corsmiddleware + settings.get("MIDDLEWARE", [])
    return data


def configure_pulp_ansible(settings: Dynaconf) -> dict[str, Any]:
    # Translate the galaxy default base path to the pulp ansible default base path.
    distro_path = settings.get("GALAXY_API_DEFAULT_DISTRIBUTION_BASE_PATH", "published")

    return {
        # ANSIBLE_URL_NAMESPACE tells pulp_ansible to generate hrefs and redirects that
        # point to the galaxy_ng api namespace. We're forcing it to get set to our api
        # namespace here because setting it to anything else will break our api.
        "ANSIBLE_URL_NAMESPACE": "galaxy:api:v3:",
        "ANSIBLE_DEFAULT_DISTRIBUTION_PATH": distro_path
    }


def configure_authentication_classes(settings: Dynaconf, data: dict[str, Any]) -> dict[str, Any]:
    # GALAXY_AUTHENTICATION_CLASSES is used to configure the galaxy api authentication
    # pretty much everywhere (on prem, cloud, dev environments, CI environments etc).
    # We need to set the REST_FRAMEWORK__DEFAULT_AUTHENTICATION_CLASSES variable so that
    # the pulp APIs use the same authentication as the galaxy APIs. Rather than setting
    # the galaxy auth classes and the DRF classes in all those environments just set the
    # default rest framework auth classes to the galaxy auth classes. Ideally we should
    # switch everything to use the default DRF auth classes, but given how many
    # environments would have to be reconfigured, this is a lot easier.

    galaxy_auth_classes = data.get(
        "GALAXY_AUTHENTICATION_CLASSES",
        settings.get("GALAXY_AUTHENTICATION_CLASSES", None)
    )
    if galaxy_auth_classes is None:
        galaxy_auth_classes = []

    # add in keycloak classes if necessary ...
    if data.get('GALAXY_AUTH_KEYCLOAK_ENABLED') is True:
        for class_name in [
            # "galaxy_ng.app.auth.session.SessionAuthentication",
            "galaxy_ng.app.auth.token.ExpiringTokenAuthentication",
            "galaxy_ng.app.auth.keycloak.KeycloakBasicAuth"
        ]:
            if class_name not in galaxy_auth_classes:
                galaxy_auth_classes.insert(0, class_name)

    # galaxy sessionauth -must- always come first ...
    galaxy_session = "galaxy_ng.app.auth.session.SessionAuthentication"
    # Check if galaxy_session is already the first element
    if galaxy_auth_classes and galaxy_auth_classes[0] != galaxy_session:
        # Remove galaxy_session if it exists in the list
        if galaxy_session in galaxy_auth_classes:
            galaxy_auth_classes.remove(galaxy_session)
        # Insert galaxy_session at the beginning of the list
        galaxy_auth_classes.insert(0, galaxy_session)

    if galaxy_auth_classes:
        data["ANSIBLE_AUTHENTICATION_CLASSES"] = list(galaxy_auth_classes)
        data["GALAXY_AUTHENTICATION_CLASSES"] = list(galaxy_auth_classes)
        data["REST_FRAMEWORK__DEFAULT_AUTHENTICATION_CLASSES"] = list(galaxy_auth_classes)

    return data


def configure_password_validators(settings: Dynaconf) -> dict[str, Any]:
    """Configure the password validators"""
    GALAXY_MINIMUM_PASSWORD_LENGTH: int = settings.get("GALAXY_MINIMUM_PASSWORD_LENGTH", 9)
    AUTH_PASSWORD_VALIDATORS: list[dict[str, Any]] = settings.AUTH_PASSWORD_VALIDATORS
    # NOTE: Dynaconf can't add or merge on dicts inside lists.
    # So we need to traverse the list to change it until the RFC is implemented
    # https://github.com/rochacbruno/dynaconf/issues/299#issuecomment-900616706
    for dict_item in AUTH_PASSWORD_VALIDATORS:
        if dict_item["NAME"].endswith("MinimumLengthValidator"):
            dict_item["OPTIONS"]["min_length"] = int(GALAXY_MINIMUM_PASSWORD_LENGTH)
    return {"AUTH_PASSWORD_VALIDATORS": AUTH_PASSWORD_VALIDATORS}


def configure_api_base_path(settings: Dynaconf) -> dict[str, Any]:
    """Set the pulp api root under the galaxy api root."""

    galaxy_api_root = settings.get("GALAXY_API_PATH_PREFIX")
    pulp_api_root = f"/{galaxy_api_root.strip('/')}/pulp/"
    return {"API_ROOT": pulp_api_root}


def configure_ldap(settings: Dynaconf) -> dict[str, Any]:
    """Configure ldap settings for galaxy.
    This function returns a dictionary that will be merged to the settings.
    """

    data = {}
    AUTH_LDAP_SERVER_URI = settings.get("AUTH_LDAP_SERVER_URI", default=None)
    AUTH_LDAP_BIND_DN = settings.get("AUTH_LDAP_BIND_DN", default=None)
    AUTH_LDAP_BIND_PASSWORD = settings.get("AUTH_LDAP_BIND_PASSWORD", default=None)
    AUTH_LDAP_USER_SEARCH_BASE_DN = settings.get("AUTH_LDAP_USER_SEARCH_BASE_DN", default=None)
    AUTH_LDAP_USER_SEARCH_SCOPE = settings.get("AUTH_LDAP_USER_SEARCH_SCOPE", default=None)
    AUTH_LDAP_USER_SEARCH_FILTER = settings.get("AUTH_LDAP_USER_SEARCH_FILTER", default=None)
    AUTH_LDAP_GROUP_SEARCH_BASE_DN = settings.get("AUTH_LDAP_GROUP_SEARCH_BASE_DN", default=None)
    AUTH_LDAP_GROUP_SEARCH_SCOPE = settings.get("AUTH_LDAP_GROUP_SEARCH_SCOPE", default=None)
    AUTH_LDAP_GROUP_SEARCH_FILTER = settings.get("AUTH_LDAP_GROUP_SEARCH_FILTER", default=None)
    AUTH_LDAP_USER_ATTR_MAP = settings.get("AUTH_LDAP_USER_ATTR_MAP", default={})

    # Add settings if LDAP Auth values are provided
    if all(
        [
            AUTH_LDAP_SERVER_URI,
            AUTH_LDAP_BIND_DN,
            AUTH_LDAP_BIND_PASSWORD,
            AUTH_LDAP_USER_SEARCH_BASE_DN,
            AUTH_LDAP_USER_SEARCH_SCOPE,
            AUTH_LDAP_USER_SEARCH_FILTER,
            AUTH_LDAP_GROUP_SEARCH_BASE_DN,
            AUTH_LDAP_GROUP_SEARCH_SCOPE,
            AUTH_LDAP_GROUP_SEARCH_FILTER,
        ]
    ):
        # The following is exposed on UI settings API to be used as a feature flag for testing.
        data["GALAXY_AUTH_LDAP_ENABLED"] = True

        global_options = settings.get("AUTH_LDAP_GLOBAL_OPTIONS", default={})

        if settings.get("GALAXY_LDAP_SELF_SIGNED_CERT"):
            global_options[ldap.OPT_X_TLS_REQUIRE_CERT] = ldap.OPT_X_TLS_NEVER

        data["AUTH_LDAP_GLOBAL_OPTIONS"] = global_options

        AUTH_LDAP_SCOPE_MAP = {
            "BASE": ldap.SCOPE_BASE,
            "ONELEVEL": ldap.SCOPE_ONELEVEL,
            "SUBTREE": ldap.SCOPE_SUBTREE,
        }

        if not settings.get("AUTH_LDAP_USER_SEARCH"):
            user_scope = AUTH_LDAP_SCOPE_MAP.get(AUTH_LDAP_USER_SEARCH_SCOPE, ldap.SCOPE_SUBTREE)
            data["AUTH_LDAP_USER_SEARCH"] = LDAPSearch(
                AUTH_LDAP_USER_SEARCH_BASE_DN,
                user_scope,
                AUTH_LDAP_USER_SEARCH_FILTER
            )

        if not settings.get("AUTH_LDAP_GROUP_SEARCH"):
            group_scope = AUTH_LDAP_SCOPE_MAP.get(AUTH_LDAP_GROUP_SEARCH_SCOPE, ldap.SCOPE_SUBTREE)
            data["AUTH_LDAP_GROUP_SEARCH"] = LDAPSearch(
                AUTH_LDAP_GROUP_SEARCH_BASE_DN,
                group_scope,
                AUTH_LDAP_GROUP_SEARCH_FILTER
            )

        # Depending on the LDAP server the following might need to be changed
        # options: https://django-auth-ldap.readthedocs.io/en/latest/groups.html#types-of-groups
        # default is set to GroupOfNamesType
        # data["AUTH_LDAP_GROUP_TYPE"] = GroupOfNamesType(name_attr="cn")
        # export PULP_AUTH_LDAP_GROUP_TYPE_CLASS="django_auth_ldap.config:GroupOfNamesType"
        if classpath := settings.get(
            "AUTH_LDAP_GROUP_TYPE_CLASS",
            default="django_auth_ldap.config:GroupOfNamesType"
        ):
            entry_point = EntryPoint(
                name=None, group=None, value=classpath
            )
            group_type_class = entry_point.load()
            group_type_params = settings.get(
                "AUTH_LDAP_GROUP_TYPE_PARAMS",
                default={"name_attr": "cn"}
            )
            data["AUTH_LDAP_GROUP_TYPE"] = group_type_class(**group_type_params)

        if isinstance(AUTH_LDAP_USER_ATTR_MAP, str):
            try:
                data["AUTH_LDAP_USER_ATTR_MAP"] = json.loads(AUTH_LDAP_USER_ATTR_MAP)
            except Exception:
                data["AUTH_LDAP_USER_ATTR_MAP"] = {}

        if settings.get("GALAXY_LDAP_LOGGING"):
            data["LOGGING"] = {
                "dynaconf_merge": True,
                "version": 1,
                "disable_existing_loggers": False,
                "handlers": {"console": {"class": "logging.StreamHandler"}},
                "loggers": {"django_auth_ldap": {"level": "DEBUG", "handlers": ["console"]}},
            }

        connection_options = settings.get("AUTH_LDAP_CONNECTION_OPTIONS", {})
        if settings.get("GALAXY_LDAP_DISABLE_REFERRALS"):
            connection_options[ldap.OPT_REFERRALS] = 0
        data["AUTH_LDAP_CONNECTION_OPTIONS"] = connection_options

        if settings.get("GALAXY_LDAP_MIRROR_ONLY_EXISTING_GROUPS"):
            data["AUTH_LDAP_MIRROR_GROUPS"] = True
            data["AUTH_LDAP_MIRROR_GROUPS_EXCEPT"] = None

    return data


def configure_authentication_backends(settings: Dynaconf, data: dict[str, Any]) -> dict[str, Any]:
    """Configure authentication backends for galaxy.

    This adds backends in the following order:
        1) default backends from pulp & settings.py
        2) any backends added to data['AUTHENTICATION_BACKENDS'] by previous hooks
        3) user AUTHENTICATION_BACKEND_PRESET to add additional backends
           from the 'presets' defined in settings.py for ldap & keycloak
        4) The backend required by https://github.com/ansible/django-ansible-base/pull/611
    """

    # start with the default pulp settings
    backends = settings.get("AUTHENTICATION_BACKENDS", [])

    # merge in backends set by the previous hooks ...
    if (default_list := data.get("AUTHENTICATION_BACKENDS")) is not None:
        backends.extend([item for item in default_list if item not in backends])

    # Load preset data for deployment specific backends
    preset_name = settings.get("AUTHENTICATION_BACKEND_PRESET")
    if (preset_list := settings.AUTHENTICATION_BACKEND_PRESETS_DATA.get(preset_name)) is not None:
        backends.extend([item for item in preset_list if item not in backends])

    # insert the AAP migrated user backend
    prefix_backend = "ansible_base.lib.backends.prefixed_user_auth.PrefixedUserAuthBackend"
    if prefix_backend not in backends:
        backends.append(prefix_backend)

    # deduplicate dynaconf_merge ...
    if backends.count("dynaconf_merge") > 1:
        backends = [x for x in backends if x != 'dynaconf_merge']
        backends.append('dynaconf_merge')

    # if there are backends, add them to the final result
    if len(backends) > 0:
        data["AUTHENTICATION_BACKENDS"] = backends

    return data


def configure_renderers(settings) -> dict[str, Any]:
    """
        Add CustomBrowsableAPI only for community (galaxy.ansible.com, galaxy-stage, galaxy-dev)"
    """
    if re.search(
        r'galaxy(-dev|-stage)*.ansible.com', settings.get('CONTENT_ORIGIN', "")
    ):
        value = settings.get("REST_FRAMEWORK__DEFAULT_RENDERER_CLASSES", [])
        value.append('galaxy_ng.app.renderers.CustomBrowsableAPIRenderer')
        return {"REST_FRAMEWORK__DEFAULT_RENDERER_CLASSES": value}

    return {}


def configure_legacy_roles(settings: Dynaconf) -> dict[str, Any]:
    """Set the feature flag for legacy roles from the setting"""
    data = {}
    legacy_roles = settings.get("GALAXY_ENABLE_LEGACY_ROLES", False)
    data["GALAXY_FEATURE_FLAGS__legacy_roles"] = legacy_roles
    return data


def validate(settings: Dynaconf) -> None:
    """Validate the configuration, raise ValidationError if invalid"""
    settings.validators.register(
        Validator(
            "GALAXY_REQUIRE_SIGNATURE_FOR_APPROVAL",
            eq=False,
            when=Validator(
                "GALAXY_REQUIRE_CONTENT_APPROVAL", eq=False,
            ),
            messages={
                "operations": "{name} cannot be True if GALAXY_REQUIRE_CONTENT_APPROVAL is False"
            },
        ),
    )

    # AUTHENTICATION BACKENDS
    presets = settings.get("AUTHENTICATION_BACKEND_PRESETS_DATA", {})
    settings.validators.register(
        Validator(
            "AUTHENTICATION_BACKEND_PRESET",
            is_in=["local", "custom", *presets.keys()],
        )
    )

    settings.validators.validate()


def configure_dynamic_settings(settings: Dynaconf) -> dict[str, Any]:
    """Dynaconf 3.2.2 allows registration of hooks on methods `get` and `as_dict`

    For galaxy this enables the Dynamic Settings feature, which triggers a
    specified function after every key is accessed.

    So after the normal get process, the registered hook will be able to
    change the value before it is returned allowing reading overrides from
    database and cache.
    """
    # we expect a list of function names here, which have to be in scope of
    # locals() for this specific file
    enabled_hooks = settings.get("DYNACONF_AFTER_GET_HOOKS")
    if not enabled_hooks:
        return {}

    # Perform lazy imports here to avoid breaking when system runs with older
    # dynaconf versions
    try:
        from dynaconf import DynaconfFormatError, DynaconfParseError
        from dynaconf.base import Settings
        from dynaconf.hooking import Action, Hook, HookValue
        from dynaconf.loaders.base import SourceMetadata
    except ImportError as exc:
        # Graceful degradation for dynaconf < 3.2.3 where  method hooking is not available
        logger.error(
            "Galaxy Dynamic Settings requires Dynaconf >=3.2.3, "
            "system will work normally but dynamic settings from database will be ignored: %s",
            str(exc)
        )
        return {}

    logger.info("Enabling Dynamic Settings Feature")

    def read_settings_from_cache_or_db(
        temp_settings: Settings,
        value: HookValue,
        key: str,
        *args,
        **kwargs
    ) -> Any:
        """A function to be attached on Dynaconf Afterget hook.
        Load everything from settings cache or db, process parsing and mergings,
        returns the desired key value
        """
        if not apps.ready or key.upper() not in DYNAMIC_SETTINGS_SCHEMA:
            # If app is starting up or key is not on allowed list bypass and just return the value
            return value.value

        # lazy import because it can't happen before apps are ready
        from galaxy_ng.app.tasks.settings_cache import (
            get_settings_from_cache,
            get_settings_from_db,
        )
        if data := get_settings_from_cache():
            metadata = SourceMetadata(loader="hooking", identifier="cache")
        else:
            data = get_settings_from_db()
            if data:
                metadata = SourceMetadata(loader="hooking", identifier="db")

        # This is the main part, it will update temp_settings with data coming from settings db
        # and by calling update it will process dynaconf parsing and merging.
        try:
            if data:
                temp_settings.update(data, loader_identifier=metadata, tomlfy=True)
        except (DynaconfFormatError, DynaconfParseError) as exc:
            logger.error("Error loading dynamic settings: %s", str(exc))

        if not data:
            logger.debug("Dynamic settings are empty, reading key %s from default sources", key)
        elif key in [_k.split("__")[0] for _k in data]:
            logger.debug("Dynamic setting for key: %s loaded from %s", key, metadata.identifier)
        else:
            logger.debug(
                "Key %s not on db/cache, %s other keys loaded from %s",
                key, len(data), metadata.identifier
            )

        return temp_settings.get(key, value.value)

    def alter_hostname_settings(
        temp_settings: Settings,
        value: HookValue,
        key: str,
        *args,
        **kwargs
    ) -> Any:
        """Use the request headers to dynamically alter the content origin and api hostname.
        This is useful in scenarios where the hub is accessible directly and through a
        reverse proxy.
        """

        # we only want to modify these settings base on request headers
        ALLOWED_KEYS = ['CONTENT_ORIGIN', 'ANSIBLE_API_HOSTNAME', 'TOKEN_SERVER']

        # If app is starting up or key is not on allowed list bypass and just return the value
        if not apps.ready or key.upper() not in ALLOWED_KEYS:
            return value.value

        # we have to assume the proxy or the edge device(s) set these headers correctly
        req = get_current_request()
        if req is not None:
            headers = dict(req.headers)
            proto = headers.get("X-Forwarded-Proto", "http")
            host = headers.get("Host", "localhost:5001")
            baseurl = proto + "://" + host
            if key.upper() == 'TOKEN_SERVER':
                baseurl += '/token/'
            return baseurl

        return value.value

    # avoid scope errors by not using a list comprehension
    hook_functions = []
    for func_name in enabled_hooks:
        hook_functions.append(Hook(locals()[func_name]))

    return {
        "_registered_hooks": {
            Action.AFTER_GET: hook_functions
        }
    }


def configure_dab_required_settings(settings: Dynaconf, data: dict) -> dict[str, Any]:
    # Create a Dynaconf object from DAB
    dab_dynaconf = factory("", "HUB", add_dab_settings=False)
    # update it with raw pulp settings
    dab_dynaconf.update(settings.as_dict())
    # Add overrides from the previous hooks
    dab_dynaconf.update(data)
    # Temporary add jwt_consumer to the installed apps because it is required by DAB
    dab_dynaconf.set("INSTALLED_APPS", "@merge_unique ansible_base.jwt_consumer")
    # Load the DAB settings that are conditionally added based on INSTALLED_APPS
    load_dab_settings(dab_dynaconf)
    # Remove jwt_consumer from the installed apps because galaxy uses Pulp implementation
    dab_dynaconf.set(
        "INSTALLED_APPS",
        [app for app in dab_dynaconf.INSTALLED_APPS if app != 'ansible_base.jwt_consumer']
    )
    # Load the standard AAP settings files
    load_standard_settings_files(dab_dynaconf)  # /etc/ansible-automation-platform/*.yaml

    # Load of the envvars prefixed with HUB_ is currently disabled
    # because galaxy right now uses PULP_ as prefix and we want to avoid confusion
    # Also some CI environments are already using HUB_ prefix to set unrelated variables
    # load_envvars(dab_dynaconf)

    # Validate the settings
    dab_validate(dab_dynaconf)
    # Get raw dict to return
    data = dab_dynaconf.as_dict()
    # to use with `dynaconf -i pulpcore.app.settings.DAB_DYNACONF [command]`
    data["DAB_DYNACONF"] = dab_dynaconf
    return data


def configure_dynaconf_cli(pulp_settings: Dynaconf, data: dict) -> dict[str, Any]:
    """Add an instance of Dynaconf to be used by the CLI.
    This instance merges metadata from the PULP and DAB dynaconf instances.

    This doesn't affect the running application, it only affects the `dynaconf` CLI.
    """
    # This is the instance that will be discovered by dynaconf CLI
    try:
        dab_dynaconf = data["DAB_DYNACONF"]
    except KeyError:
        raise AttributeError(
            "DAB_DYNACONF not found in settings "
            "configure_dab_required_settings "
            "must be called before configure_dynaconf_cli"
        )

    # Load data from both instances
    cli_dynaconf = Dynaconf(core_loaders=[], envvar_prefix=None)
    cli_dynaconf._store.update(pulp_settings._store)
    cli_dynaconf._store.update(data)

    # Merge metadata from both instances
    # first pulp
    cli_dynaconf._loaded_files = pulp_settings._loaded_files
    cli_dynaconf._loaded_hooks = pulp_settings._loaded_hooks
    cli_dynaconf._loaded_by_loaders = pulp_settings._loaded_by_loaders
    cli_dynaconf._loaded_envs = pulp_settings._loaded_envs
    # then dab
    cli_dynaconf._loaded_files.extend(dab_dynaconf._loaded_files)
    cli_dynaconf._loaded_hooks.update(dab_dynaconf._loaded_hooks)
    cli_dynaconf._loaded_by_loaders.update(dab_dynaconf._loaded_by_loaders)
    cli_dynaconf._loaded_envs.extend(dab_dynaconf._loaded_envs)

    # Add the hook to the history
    cli_dynaconf._loaded_hooks[__name__] = {"post": data}

    # assign to the CLI can discover it
    data["CLI_DYNACONF"] = cli_dynaconf

    return data
