import abc
import logging
import os
import time

import bson
import jwe
import markupsafe
import requests
from boxsdk import Client, OAuth2
from boxsdk.exception import BoxAPIException
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.postgres.fields import ArrayField  # replace with sqlite equiv?
from django.core.exceptions import ValidationError
from django.db import connections, models
from django.db.models import DateTimeField, ForeignKey, TextField
from django.db.models.query import QuerySet
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseNotFound,
)
from django.utils import timezone
from django_extensions.db.models import TimeStampedModel
from oauthlib.oauth2 import InvalidGrantError
from urllib3.exceptions import MaxRetryError

import charon.serializer as charon_serializer
import charon.settings as charon_settings
import charon.utils as charon_utils

logger = logging.getLogger(__name__)

SENSITIVE_DATA_KEY = jwe.kdf(
    charon_settings.SENSITIVE_DATA_SECRET.encode('utf-8'),
    charon_settings.SENSITIVE_DATA_SALT.encode('utf-8'),
)


# Create your models here.


def generate_object_id():
    return str(bson.ObjectId())


def ensure_bytes(value):
    """Helper function to ensure all inputs are encoded to the proper value utf-8 value
    regardless of input type"""
    if isinstance(value, bytes):
        return value
    return value.encode('utf-8')


def ensure_str(value):
    if isinstance(value, bytes):
        return value.decode()
    return value


def encrypt_string(value, prefix='jwe:::'):
    prefix = ensure_bytes(prefix)
    if value:
        value = ensure_bytes(value)
        if value and not value.startswith(prefix):
            value = (prefix + jwe.encrypt(value, SENSITIVE_DATA_KEY)).decode()
    return value


def decrypt_string(value, prefix='jwe:::'):
    prefix = ensure_bytes(prefix)
    if value:
        value = ensure_bytes(value)
        if value.startswith(prefix):
            value = jwe.decrypt(value[len(prefix) :], SENSITIVE_DATA_KEY).decode()
    return value


class NaiveDatetimeException(Exception):
    pass


class EncryptedTextField(TextField):
    """
    This field transparently encrypts data in the database. It should probably only be
    used with PG unless the user takes into account the db specific trade-offs with
    TextFields.
    """

    prefix = 'jwe:::'

    def get_db_prep_value(self, value, **kwargs):
        return encrypt_string(value, prefix=self.prefix)

    def to_python(self, value):
        return decrypt_string(value, prefix=self.prefix)

    def from_db_value(self, value, expression, connection):
        return self.to_python(value)


class NonNaiveDateTimeField(DateTimeField):
    def get_prep_value(self, value):
        value = super(NonNaiveDateTimeField, self).get_prep_value(value)
        if value is not None and (
            value.tzinfo is None or value.tzinfo.utcoffset(value) is None
        ):
            raise NaiveDatetimeException('Tried to encode a naive datetime.')
        return value


class QuerySetExplainMixin:
    def explain(self, *args):
        extra_arguments = ''
        for item in args:
            extra_arguments = (
                '{} {}'.format(extra_arguments, item)
                if isinstance(item, str)
                else extra_arguments
            )
        cursor = connections[self.db].cursor()
        query, params = self.query.sql_with_params()
        cursor.execute('explain analyze verbose %s' % query, params)
        return '\n'.join(r[0] for r in cursor.fetchall())


QuerySet = type('QuerySet', (QuerySetExplainMixin, QuerySet), dict(QuerySet.__dict__))


class BaseModel(TimeStampedModel, QuerySetExplainMixin):
    migration_page_size = 50000

    objects = models.QuerySet.as_manager()

    class Meta:
        abstract = True

    def __unicode__(self):
        return '{}'.format(self.id)

    def to_storage(self, include_auto_now=True):
        local_django_fields = set(
            [
                x.name
                for x in self._meta.concrete_fields
                if include_auto_now or not getattr(x, 'auto_now', False)
            ]
        )
        return {name: self.serializable_value(name) for name in local_django_fields}

    @classmethod
    def get_fk_field_names(cls):
        return [
            field.name
            for field in cls._meta.get_fields()
            if field.is_relation
            and not field.auto_created
            and (field.many_to_one or field.one_to_one)
            and not isinstance(field, GenericForeignKey)
        ]

    @classmethod
    def get_m2m_field_names(cls):
        return [
            field.attname or field.name
            for field in cls._meta.get_fields()
            if field.is_relation and field.many_to_many and not hasattr(field, 'field')
        ]

    @classmethod
    def load(cls, data, select_for_update=False):
        try:
            return (
                cls.objects.get(pk=data)
                if not select_for_update
                else cls.objects.filter(pk=data).select_for_update().get()
            )
        except cls.DoesNotExist:
            return None

    @property
    def _primary_name(self):
        return '_id'

    @property
    def _is_loaded(self):
        return bool(self.pk)

    def reload(self):
        return self.refresh_from_db()

    def refresh_from_db(self, **kwargs):
        super(BaseModel, self).refresh_from_db(**kwargs)
        # Since Django 2.2, any cached relations are cleared from the reloaded instance.
        #
        # See https://docs.djangoproject.com/en/2.2/ref/models/instances/#django.db.models.Model.refresh_from_db  # noqa: E501
        #
        # However, the default `refresh_from_db()` doesn't refresh related fields.
        # Neither can we refresh related field(s) since it will inevitably cause
        # infinite loop; and Many/One-to-Many relations add to the complexity.
        #
        # The recommended behavior is to explicitly refresh the fields when necessary.
        # In order to preserve pre-upgrade behavior, our customization only reloads GFKs
        for f in self._meta._get_fields(reverse=False):
            # Note: the following `if` condition is how django internally identifies GFK
            if (
                f.is_relation
                and f.many_to_one
                and not (hasattr(f.remote_field, 'model') and f.remote_field.model)
            ):
                if hasattr(self, f.name):
                    try:
                        getattr(self, f.name).refresh_from_db()
                    except AttributeError:
                        continue

    def clone(self):
        """Create a new, unsaved copy of this object."""
        copy = self.__class__.objects.get(pk=self.pk)
        copy.id = None

        # empty all the fks
        fk_field_names = [
            f.name
            for f in self._meta.model._meta.get_fields()
            if isinstance(f, (ForeignKey, GenericForeignKey))
        ]
        for field_name in fk_field_names:
            setattr(copy, field_name, None)

        try:
            copy._id = bson.ObjectId()
        except AttributeError:
            pass
        return copy

    def save(self, *args, **kwargs):
        # Make Django validate on save (like modm)
        if kwargs.pop('clean', True) and not (
            kwargs.get('force_insert') or kwargs.get('force_update')
        ):
            try:
                self.full_clean()
            except ValidationError as err:
                raise ValidationError(*err.args)
        return super(BaseModel, self).save(*args, **kwargs)


class BaseIDMixin(models.Model):
    class Meta:
        abstract = True


class ObjectIDMixin(BaseIDMixin):
    primary_identifier_name = '_id'

    _id = models.CharField(
        max_length=24, default=generate_object_id, unique=True, db_index=True
    )

    def __unicode__(self):
        return '_id: {}'.format(self._id)

    @classmethod
    def load(cls, q, select_for_update=False):
        try:
            return (
                cls.objects.get(_id=q)
                if not select_for_update
                else cls.objects.filter(_id=q).select_for_update().get()
            )
        except cls.DoesNotExist:
            # modm doesn't throw exceptions when loading things that don't exist
            return None

    class Meta:
        abstract = True


class ExternalProviderMeta(abc.ABCMeta):
    """Keeps track of subclasses of the ``ExternalProvider`` object"""

    def __init__(cls, name, bases, dct):
        super(ExternalProviderMeta, cls).__init__(name, bases, dct)
        if not isinstance(cls.short_name, abc.abstractproperty):
            PROVIDER_LOOKUP[cls.short_name] = cls


class ExternalProvider(object, with_metaclass(ExternalProviderMeta)):
    """A connection to an external service (ex: GitHub).

    This object contains no credentials, and is not saved in the database.
    It provides an unauthenticated session with the provider, unless ``account``
    has been set - in which case, it provides a connection authenticated as the
    ``ExternalAccount`` instance.

    Conceptually, this can be thought of as an extension of ``ExternalAccount``.
    It's a separate object because this must be subclassed for each provider,
    and ``ExternalAccount`` instances are stored within a single collection.
    """

    # Default to OAuth v2.0.
    _oauth_version = OAUTH2

    # Providers that have expiring tokens must override these
    auto_refresh_url = None
    refresh_time = 0  # When to refresh the oauth_key (seconds)
    expiry_time = 0  # If/When the refresh token expires (seconds). 0 indicates a non-expiring refresh token

    def __init__(self, account=None):
        super(ExternalProvider, self).__init__()

        # provide an unauthenticated session by default
        self.account = account

    def __repr__(self):
        return '<{name}: {status}>'.format(
            name=self.__class__.__name__,
            status=self.account.provider_id if self.account else 'anonymous',
        )

    @abc.abstractproperty
    def auth_url_base(self):
        """The base URL to begin the OAuth dance"""
        pass

    @property
    def auth_url(self):
        """The URL to begin the OAuth dance.

        This property method has side effects - it at least adds temporary
        information to the session so that callbacks can be associated with
        the correct user.  For OAuth1, it calls the provider to obtain
        temporary credentials to start the flow.
        """
        current_session = get_session()
        # create a dict on the session object if it's not already there
        if current_session.get('oauth_states', None) is None:
            current_session['oauth_states'] = {}

        if self._oauth_version == OAUTH2:
            # Quirk: Some time between 2019/05/31 and 2019/06/04, Bitbucket's OAuth2 API no longer
            #        expects the query param `redirect_uri` in the `oauth2/authorize` endpoint.  In
            #        addition, it relies on the "Callback URL" of the "OAuth Consumer" to redirect
            #        the auth flow after successful authorization.  `ADDONS_OAUTH_NO_REDIRECT` is a
            #        list containing addons that do not use `redirect_uri` in OAuth2 requests.
            if self.short_name in ADDONS_OAUTH_NO_REDIRECT:
                redirect_uri = None
            else:
                redirect_uri = charon_utils.web_url_for(
                    'oauth_callback', service_name=self.short_name, _absolute=True
                )
            # build the URL
            oauth = OAuth2Session(
                self.client_id,
                redirect_uri=redirect_uri,
                scope=self.default_scopes,
            )

            url, state = oauth.authorization_url(self.auth_url_base)

            # save state token to the session for confirmation in the callback
            current_session['oauth_states'][self.short_name] = {'state': state}

        elif self._oauth_version == OAUTH1:
            # get a request token
            oauth = OAuth1Session(
                client_key=self.client_id,
                client_secret=self.client_secret,
            )

            # request temporary credentials from the provider
            response = oauth.fetch_request_token(self.request_token_url)

            # store them in the session for use in the callback
            current_session['oauth_states'][self.short_name] = {
                'token': response.get('oauth_token'),
                'secret': response.get('oauth_token_secret'),
            }

            url = oauth.authorization_url(self.auth_url_base)

        current_session.save()
        return url

    @abc.abstractproperty
    def callback_url(self):
        """The provider URL to exchange the code for a token"""
        pass

    @abc.abstractproperty
    def client_id(self):
        """OAuth Client ID. a/k/a: Application ID"""
        pass

    @abc.abstractproperty
    def client_secret(self):
        """OAuth Client Secret. a/k/a: Application Secret, Application Key"""
        pass

    default_scopes = list()

    @abc.abstractproperty
    def name(self):
        """Human-readable name of the service. e.g.: ORCiD, GitHub"""
        pass

    @abc.abstractproperty
    def short_name(self):
        """Name of the service to be used internally. e.g.: orcid, github"""
        pass

    def auth_callback(self, user, **kwargs):
        """Exchange temporary credentials for permanent credentials

        This is called in the view that handles the user once they are returned
        to the OSF after authenticating on the external service.
        """
        current_session = get_session()
        if 'error' in request.args:
            return False

        # make sure the user has temporary credentials for this provider
        try:
            cached_credentials = current_session['oauth_states'][self.short_name]
        except KeyError:
            raise PermissionsError('OAuth flow not recognized.')

        if self._oauth_version == OAUTH1:
            request_token = request.args.get('oauth_token')

            # make sure this is the same user that started the flow
            if cached_credentials.get('token') != request_token:
                raise PermissionsError('Request token does not match')

            response = OAuth1Session(
                client_key=self.client_id,
                client_secret=self.client_secret,
                resource_owner_key=cached_credentials.get('token'),
                resource_owner_secret=cached_credentials.get('secret'),
                verifier=request.args.get('oauth_verifier'),
            ).fetch_access_token(self.callback_url)

        elif self._oauth_version == OAUTH2:
            state = request.args.get('state')

            # make sure this is the same user that started the flow
            if cached_credentials.get('state') != state:
                raise PermissionsError('Request token does not match')

            try:
                # Quirk: Similarly to the `oauth2/authorize` endpoint, the `oauth2/access_token`
                #        endpoint of Bitbucket would fail if a not-none or non-empty `redirect_uri`
                #        were provided in the body of the POST request.
                if self.short_name in ADDONS_OAUTH_NO_REDIRECT:
                    redirect_uri = None
                else:
                    redirect_uri = charon_utils.web_url_for(
                        'oauth_callback', service_name=self.short_name, _absolute=True
                    )
                response = OAuth2Session(
                    self.client_id,
                    redirect_uri=redirect_uri,
                ).fetch_token(
                    self.callback_url,
                    client_secret=self.client_secret,
                    code=request.args.get('code'),
                )
            except (MissingTokenError, RequestsHTTPError):
                raise HTTPError(http_status.HTTP_503_SERVICE_UNAVAILABLE)
        # pre-set as many values as possible for the ``ExternalAccount``
        info = self._default_handle_callback(response)
        # call the hook for subclasses to parse values from the response
        info.update(self.handle_callback(response))

        return self._set_external_account(user, info)

    def _set_external_account(self, user, info):
        current_session = get_session()
        self.account, created = ExternalAccount.objects.get_or_create(
            provider=self.short_name,
            provider_id=info['provider_id'],
        )

        # ensure that provider_name is correct
        self.account.provider_name = self.name
        # required
        self.account.oauth_key = info['key']

        # only for OAuth1
        self.account.oauth_secret = info.get('secret')

        # only for OAuth2
        self.account.expires_at = info.get('expires_at')
        self.account.refresh_token = info.get('refresh_token')
        self.account.date_last_refreshed = timezone.now()

        # additional information
        self.account.display_name = info.get('display_name')
        self.account.profile_url = info.get('profile_url')

        self.account.save()

        # add it to the user's list of ``ExternalAccounts``
        if not user.external_accounts.filter(id=self.account.id).exists():
            user.external_accounts.add(self.account)
            user.save()

        if self.short_name in current_session.get('oauth_states', {}):
            del current_session['oauth_states'][self.short_name]
            current_session.save()

        return True

    def _default_handle_callback(self, data):
        """Parse as much out of the key exchange's response as possible.

        This should not be over-ridden in subclasses.
        """
        if self._oauth_version == OAUTH1:
            key = data.get('oauth_token')
            secret = data.get('oauth_token_secret')

            values = {}

            if key:
                values['key'] = key
            if secret:
                values['secret'] = secret

            return values

        elif self._oauth_version == OAUTH2:
            key = data.get('access_token')
            refresh_token = data.get('refresh_token')
            expires_at = data.get('expires_at')
            scopes = data.get('scope')

            values = {}

            if key:
                values['key'] = key
            if scopes:
                values['scope'] = scopes
            if refresh_token:
                values['refresh_token'] = refresh_token
            if expires_at:
                values['expires_at'] = dt.datetime.fromtimestamp(float(expires_at))

            return values

    @abc.abstractmethod
    def handle_callback(self, response):
        """Hook for allowing subclasses to parse information from the callback.

        Subclasses should implement this method to provide `provider_id`
        and `profile_url`.

        Values provided by ``self._default_handle_callback`` can be over-ridden
        here as well, in the unexpected case that they are parsed incorrectly
        by default.

        :param response: The JSON returned by the provider during the exchange
        :return dict:
        """
        pass

    def refresh_oauth_key(
        self,
        force=False,
        extra=None,
        resp_auth_token_key='access_token',
        resp_refresh_token_key='refresh_token',
        resp_expiry_fn=None,
    ):
        """Handles the refreshing of an oauth_key for account associated with this provider.
           Not all addons need to use this, as some do not have oauth_keys that expire.

        Subclasses must define the following for this functionality:
        `auto_refresh_url` - URL to use when refreshing tokens. Must use HTTPS
        `refresh_time` - Time (in seconds) that the oauth_key should be refreshed after.
                            Typically half the duration of validity. Cannot be 0.

        Providers may have different keywords in their response bodies, kwargs
        `resp_*_key` allow subclasses to override these if necessary.

        kwarg `resp_expiry_fn` allows subclasses to specify a function that will return the
        datetime-formatted oauth_key expiry key, given a successful refresh response from
        `auto_refresh_url`. A default using 'expires_at' as a key is provided.
        """
        extra = extra or {}
        # Ensure this is an authenticated Provider that uses token refreshing
        if not (self.account and self.auto_refresh_url):
            return False

        # Ensure this Provider is for a valid addon
        if not (self.client_id and self.client_secret):
            return False

        # Ensure a refresh is needed
        if not (force or self._needs_refresh()):
            return False

        if self.has_expired_credentials and not force:
            return False

        resp_expiry_fn = resp_expiry_fn or (
            lambda x: timezone.now()
            + timezone.timedelta(seconds=float(x['expires_in']))
        )

        client = OAuth2Session(
            self.client_id,
            token={
                'access_token': self.account.oauth_key,
                'refresh_token': self.account.refresh_token,
                'token_type': 'Bearer',
                'expires_in': '-30',
            },
        )

        extra.update({'client_id': self.client_id, 'client_secret': self.client_secret})

        try:
            token = client.refresh_token(self.auto_refresh_url, **extra)
        except (AccessDeniedError, InvalidGrantError, TokenExpiredError):
            if not force:
                return False
            else:
                raise

        self.account.oauth_key = token[resp_auth_token_key]
        self.account.refresh_token = token[resp_refresh_token_key]
        self.account.expires_at = resp_expiry_fn(token)
        self.account.date_last_refreshed = timezone.now()
        self.account.save()
        return True

    def _needs_refresh(self):
        """Determines whether or not an associated ExternalAccount needs
        a oauth_key.

        return bool: True if needs_refresh
        """
        if self.refresh_time and self.account.expires_at:
            return (
                self.account.expires_at - timezone.now()
            ).total_seconds() < self.refresh_time
        return False

    @property
    def has_expired_credentials(self):
        """Determines whether or not an associated ExternalAccount has
        expired credentials that can no longer be renewed

        return bool: True if cannot be refreshed
        """
        if self.expiry_time and self.account.expires_at:
            return (
                timezone.now() - self.account.expires_at
            ).total_seconds() > self.expiry_time
        return False


class ExternalAccount(ObjectIDMixin, BaseModel):
    """An account on an external service.

    Note that this object is not and should not be aware of what other objects
    are associated with it. This is by design, and this object should be kept as
    thin as possible, containing only those fields that must be stored in the
    database.

    The ``provider`` field is a de facto foreign key to an ``ExternalProvider``
    object, as providers are not stored in the database.
    """

    # The OAuth credentials. One or both of these fields should be populated.
    # For OAuth1, this is usually the "oauth_token"
    # For OAuth2, this is usually the "access_token"
    oauth_key = EncryptedTextField(blank=True, null=True)

    # For OAuth1, this is usually the "oauth_token_secret"
    # For OAuth2, this is not used
    oauth_secret = EncryptedTextField(blank=True, null=True)

    # Used for OAuth2 only
    refresh_token = EncryptedTextField(blank=True, null=True)
    date_last_refreshed = NonNaiveDateTimeField(blank=True, null=True)
    expires_at = NonNaiveDateTimeField(blank=True, null=True)
    scopes = ArrayField(models.CharField(max_length=128), default=list, blank=True)

    # The `name` of the service
    # This lets us query for only accounts on a particular provider
    # TODO We should make provider an actual FK someday.
    provider = models.CharField(max_length=50, blank=False, null=False)
    # The proper 'name' of the service
    # Needed for account serialization
    provider_name = models.CharField(max_length=255, blank=False, null=False)

    # The unique, persistent ID on the remote service.
    provider_id = models.CharField(max_length=255, blank=False, null=False)

    # The user's name on the external service
    display_name = EncryptedTextField(blank=True, null=True)
    # A link to the user's profile on the external service
    profile_url = EncryptedTextField(blank=True, null=True)

    def __repr__(self):
        return '<ExternalAccount: {}/{}>'.format(self.provider, self.provider_id)

    def _natural_key(self):
        if self.pk:
            return self.pk
        return hash(str(self.provider_id) + str(self.provider))

    class Meta:
        unique_together = [
            (
                'provider',
                'provider_id',
            )
        ]


class Provider(ExternalProvider):
    name = 'Box'
    short_name = 'box'

    client_id = charon_settings.BOX_KEY
    client_secret = charon_settings.BOX_SECRET

    auth_url_base = charon_settings.BOX_OAUTH_AUTH_ENDPOINT
    callback_url = charon_settings.BOX_OAUTH_TOKEN_ENDPOINT
    auto_refresh_url = callback_url
    refresh_time = charon_settings.REFRESH_TIME
    expiry_time = charon_settings.EXPIRY_TIME
    default_scopes = ['root_readwrite']

    def handle_callback(self, response):
        """View called when the Oauth flow is completed. Adds a new UserSettings
        record to the user and saves the user's access token and account info.
        """

        client = Client(
            OAuth2(
                access_token=response['access_token'],
                refresh_token=response['refresh_token'],
                client_id=charon_settings.BOX_KEY,
                client_secret=charon_settings.BOX_SECRET,
            )
        )

        about = client.user().get()

        return {
            'provider_id': about['id'],
            'display_name': about['name'],
            'profile_url': 'https://app.box.com/profile/{0}'.format(about['id']),
        }


class BaseAddonSettings(ObjectIDMixin, BaseModel):
    is_deleted = models.BooleanField(default=False)
    deleted = NonNaiveDateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    @property
    def config(self):
        return self._meta.app_config

    @property
    def short_name(self):
        return self.config.short_name

    def delete(self, save=True):
        self.is_deleted = True
        self.deleted = timezone.now()
        self.on_delete()
        if save:
            self.save()

    def undelete(self, save=True):
        self.is_deleted = False
        self.deleted = None
        self.on_add()
        if save:
            self.save()

    def to_json(self, user):
        return {
            'addon_short_name': self.config.short_name,
            'addon_full_name': self.config.full_name,
        }

    #############
    # Callbacks #
    #############

    def on_add(self):
        """Called when the addon is added (or re-added) to the owner (User or Node)."""
        pass

    def on_delete(self):
        """Called when the addon is deleted from the owner (User or Node)."""
        pass


class BaseUserSettings(BaseAddonSettings):
    owner = models.OneToOneField(
        OSFUser,
        related_name='%(app_label)s_user_settings',
        blank=True,
        null=True,
        on_delete=models.CASCADE,
    )

    class Meta:
        abstract = True

    @property
    def public_id(self):
        return None

    @property
    def has_auth(self):
        """Whether the user has added credentials for this addon."""
        return False

    # TODO: Test me @asmacdo
    @property
    def nodes_authorized(self):
        """Get authorized, non-deleted nodes. Returns an empty list if the
        attached add-on does not include a node model.
        """
        model = self.config.node_settings
        if not model:
            return []
        return [
            obj.owner
            for obj in model.objects.filter(
                user_settings=self, owner__is_deleted=False
            ).select_related('owner')
        ]

    @property
    def can_be_merged(self):
        return hasattr(self, 'merge')

    def to_json(self, user):
        ret = super(BaseUserSettings, self).to_json(user)
        ret['has_auth'] = self.has_auth
        ret.update(
            {
                'nodes': [
                    {
                        '_id': node._id,
                        'url': node.url,
                        'title': node.title,
                        'registered': node.is_registration,
                        'api_url': node.api_url,
                    }
                    for node in self.nodes_authorized
                ]
            }
        )
        return ret

    def __repr__(self):
        if self.owner:
            return '<{cls} owned by user {uid}>'.format(
                cls=self.__class__.__name__, uid=self.owner._id
            )
        return '<{cls} with no owner>'.format(cls=self.__class__.__name__)


# TODO: this is a signal - what do we do?
# @oauth_complete.connect
def oauth_complete(provider, account, user):
    if not user or not account:
        return
    user.add_addon(account.provider)
    user.save()


class BaseOAuthUserSettings(BaseUserSettings):
    # Keeps track of what nodes have been given permission to use external
    #   accounts belonging to the user.
    oauth_grants = DateTimeAwareJSONField(default=dict, blank=True)
    # example:
    # {
    #     '<Node._id>': {
    #         '<ExternalAccount._id>': {
    #             <metadata>
    #         },
    #     }
    # }
    #
    # metadata here is the specific to each addon.

    # The existence of this property is used to determine whether or not
    #   an addon instance is an "OAuth addon" in
    #   AddonModelMixin.get_oauth_addons().
    oauth_provider = None

    serializer = charon_serializer.OAuthAddonSerializer  # TODO: import this?

    class Meta:
        abstract = True

    @property
    def has_auth(self):
        return self.external_accounts.exists()

    @property
    def external_accounts(self):
        """The user's list of ``ExternalAccount`` instances for this provider"""
        return self.owner.external_accounts.filter(
            provider=self.oauth_provider.short_name
        )

    def delete(self, save=True):
        for account in self.external_accounts.filter(provider=self.config.short_name):
            self.revoke_oauth_access(account, save=False)
        super(BaseOAuthUserSettings, self).delete(save=save)

    def grant_oauth_access(self, node, external_account, metadata=None):
        """Give a node permission to use an ``ExternalAccount`` instance."""
        # ensure the user owns the external_account
        if not self.owner.external_accounts.filter(id=external_account.id).exists():
            raise charon_utils.PermissionsError()

        metadata = metadata or {}

        # create an entry for the node, if necessary
        if node._id not in self.oauth_grants:
            self.oauth_grants[node._id] = {}

        # create an entry for the external account on the node, if necessary
        if external_account._id not in self.oauth_grants[node._id]:
            self.oauth_grants[node._id][external_account._id] = {}

        # update the metadata with the supplied values
        for key, value in metadata.items():
            self.oauth_grants[node._id][external_account._id][key] = value

        self.save()

    # TODO: this uses the request, yes?  why are we checking request in model
    # @must_be_logged_in
    def revoke_oauth_access(self, external_account, auth, save=True):
        """Revoke all access to an ``ExternalAccount``.

        TODO: This should accept node and metadata params in the future, to
            allow fine-grained revocation of grants. That's not yet been needed,
            so it's not yet been implemented.
        """
        for node in self.get_nodes_with_oauth_grants(external_account):
            try:
                node.get_addon(external_account.provider, is_deleted=True).deauthorize(
                    auth=auth
                )
            except AttributeError:
                # No associated addon settings despite oauth grant
                pass

        if (
            external_account.osfuser_set.count() == 1
            and external_account.osfuser_set.filter(id=auth.user.id).exists()
        ):
            # Only this user is using the account, so revoke remote access as well.
            self.revoke_remote_oauth_access(external_account)

        for key in self.oauth_grants:
            self.oauth_grants[key].pop(external_account._id, None)
        if save:
            self.save()

    def revoke_remote_oauth_access(self, external_account):
        """Makes outgoing request to remove the remote oauth grant
        stored by third-party provider.

        Individual addons must override this method, as it is addon-specific behavior.
        Not all addon providers support this through their API, but those that do
        should also handle the case where this is called with an external_account
        with invalid credentials, to prevent a user from being unable to disconnect
        an account.
        """
        pass

    def verify_oauth_access(self, node, external_account, metadata=None):
        """Verify that access has been previously granted.

        If metadata is not provided, this checks only if the node can access the
        account. This is suitable to check to see if the node's addon settings
        is still connected to an external account (i.e., the user hasn't revoked
        it in their user settings pane).

        If metadata is provided, this checks to see that all key/value pairs
        have been granted. This is suitable for checking access to a particular
        folder or other resource on an external provider.
        """

        metadata = metadata or {}

        # ensure the grant exists
        try:
            grants = self.oauth_grants[node._id][external_account._id]
        except KeyError:
            return False

        # Verify every key/value pair is in the grants dict
        for key, value in metadata.items():
            if key not in grants or grants[key] != value:
                return False

        return True

    def get_nodes_with_oauth_grants(self, external_account):
        # Generator of nodes which have grants for this external account
        for node_id, grants in self.oauth_grants.items():
            node = AbstractNode.load(node_id)
            if external_account._id in grants.keys() and not node.is_deleted:
                yield node

    def get_attached_nodes(self, external_account):
        for node in self.get_nodes_with_oauth_grants(external_account):
            if node is None:
                continue
            node_settings = node.get_addon(self.oauth_provider.short_name)

            if node_settings is None:
                continue

            if node_settings.external_account == external_account:
                yield node

    def merge(self, user_settings):
        """Merge `user_settings` into this instance"""
        if user_settings.__class__ is not self.__class__:
            raise TypeError('Cannot merge different addons')

        for node_id, data in user_settings.oauth_grants.items():
            if node_id not in self.oauth_grants:
                self.oauth_grants[node_id] = data
            else:
                node_grants = user_settings.oauth_grants[node_id].items()
                for ext_acct, meta in node_grants:
                    if ext_acct not in self.oauth_grants[node_id]:
                        self.oauth_grants[node_id][ext_acct] = meta
                    else:
                        for k, v in meta:
                            if k not in self.oauth_grants[node_id][ext_acct]:
                                self.oauth_grants[node_id][ext_acct][k] = v

        user_settings.oauth_grants = {}
        user_settings.save()

        try:
            config = charon_settings.ADDONS_AVAILABLE_DICT[
                self.oauth_provider.short_name
            ]
            Model = config.models['nodesettings']
        except KeyError:
            pass
        else:
            Model.objects.filter(user_settings=user_settings).update(user_settings=self)

        self.save()

    def to_json(self, user):
        ret = super(BaseOAuthUserSettings, self).to_json(user)

        ret['accounts'] = self.serializer(user_settings=self).serialized_accounts

        return ret

    #############
    # Callbacks #
    #############

    def on_delete(self):
        """When the user deactivates the addon, clear auth for connected nodes."""
        super(BaseOAuthUserSettings, self).on_delete()
        nodes = [AbstractNode.load(node_id) for node_id in self.oauth_grants.keys()]
        for node in nodes:
            node_addon = node.get_addon(self.oauth_provider.short_name)
            if node_addon and node_addon.user_settings == self:
                node_addon.clear_auth()


class BaseNodeSettings(BaseAddonSettings):
    owner = models.OneToOneField(
        AbstractNode,
        related_name='%(app_label)s_node_settings',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )

    class Meta:
        abstract = True

    @property
    def complete(self):
        """Whether or not this addon is properly configured
        :rtype bool:
        """
        raise NotImplementedError()

    @property
    def configured(self):
        """Whether or not this addon has had a folder connected.
        :rtype bool:
        """
        return self.complete

    @property
    def has_auth(self):
        """Whether the node has added credentials for this addon."""
        return False

    def to_json(self, user):
        ret = super(BaseNodeSettings, self).to_json(user)
        ret.update(
            {
                'user': {'permissions': self.owner.get_permissions(user)},
                'node': {
                    'id': self.owner._id,
                    'api_url': self.owner.api_url,
                    'url': self.owner.url,
                    'is_registration': self.owner.is_registration,
                },
                'node_settings_template': os.path.basename(
                    self.config.node_settings_template
                ),
            }
        )
        return ret

    #############
    # Callbacks #
    #############

    def before_page_load(self, node, user):
        """

        :param User user:
        :param Node node:

        """
        pass

    def before_remove_contributor(self, node, removed):
        """
        :param Node node:
        :param User removed:
        """
        pass

    def after_remove_contributor(self, node, removed, auth=None):
        """
        :param Node node:
        :param User removed:
        """
        pass

    def before_make_public(self, node):
        """

        :param Node node:
        :returns: Alert message or None

        """
        pass

    def before_make_private(self, node):
        """

        :param Node node:
        :returns: Alert message or None

        """
        pass

    def after_set_privacy(self, node, permissions):
        """

        :param Node node:
        :param str permissions:

        """
        pass

    def before_fork(self, node, user):
        """Return warning text to display if user auth will be copied to a
        fork.
        :param Node node:
        :param Uder user
        :returns Alert message
        """

        if hasattr(self, 'user_settings'):
            if self.user_settings is None:
                return (
                    u'Because you have not configured the {addon} add-on, your '
                    u'authentication will not be transferred to the forked {category}. '
                    u'You may authorize and configure the {addon} add-on '
                    u'in the new fork on the settings page.'
                ).format(
                    addon=self.config.full_name,
                    category=node.project_or_component,
                )

            elif self.user_settings and self.user_settings.owner == user:
                return (
                    u'Because you have authorized the {addon} add-on for this '
                    u'{category}, forking it will also transfer your authentication to '
                    u'the forked {category}.'
                ).format(
                    addon=self.config.full_name,
                    category=node.project_or_component,
                )
            else:
                return (
                    u'Because the {addon} add-on has been authorized by a different '
                    u'user, forking it will not transfer authentication to the forked '
                    u'{category}. You may authorize and configure the {addon} add-on '
                    u'in the new fork on the settings page.'
                ).format(
                    addon=self.config.full_name,
                    category=node.project_or_component,
                )

    def after_fork(self, node, fork, user, save=True):
        """

        :param Node node:
        :param Node fork:
        :param User user:
        :param bool save:
        :returns: cloned settings

        """
        clone = self.clone()
        clone.user_settings = None
        clone.owner = fork

        if save:
            clone.save()

        return clone

    def before_register(self, node, user):
        """

        :param Node node:
        :param User user:
        :returns: Alert message

        """
        pass

    def after_register(self, node, registration, user, save=True):
        """

        :param Node node:
        :param Node registration:
        :param User user:
        :param bool save:
        :returns: Tuple of cloned settings and alert message

        """
        return None, None

    def after_delete(self, user):
        """

        :param Node node:
        :param User user:

        """
        pass


class BaseStorageAddon(object):
    """
    Mixin class for traversing file trees of addons with files
    """

    root_node = GenericRootNode()

    class Meta:
        abstract = True

    @property
    def archive_folder_name(self):
        name = 'Archive of {addon}'.format(addon=self.config.full_name)
        folder_name = getattr(self, 'folder_name', '').lstrip('/').strip()
        if folder_name:
            name = name + ': {folder}'.format(folder=folder_name)
        return name

    def _get_fileobj_child_metadata(self, filenode, user, cookie=None, version=None):
        from api.base.utils import waterbutler_api_url_for

        kwargs = {}
        if version:
            kwargs['version'] = version
        if cookie:
            kwargs['cookie'] = cookie
        elif user:
            kwargs['cookie'] = user.get_or_create_cookie().decode()

        metadata_url = waterbutler_api_url_for(
            self.owner._id,
            self.config.short_name,
            path=filenode.get('path', '/'),
            user=user,
            view_only=True,
            _internal=True,
            base_url=self.owner.osfstorage_region.waterbutler_url,
            **kwargs
        )

        res = requests.get(metadata_url)

        if res.status_code != 200:
            raise HttpResponse(res.content, status=res.status_code)

        # TODO: better throttling?
        time.sleep(1.0 / 5.0)

        data = res.json().get('data', None)
        if data:
            return [child['attributes'] for child in data]
        return []

    def _get_file_tree(self, filenode=None, user=None, cookie=None, version=None):
        """
        Recursively get file metadata
        """
        filenode = filenode or {
            'path': '/',
            'kind': 'folder',
            'name': self.root_node.name,
        }
        if filenode.get('kind') == 'file':
            return filenode

        kwargs = {
            'version': version,
            'cookie': cookie,
        }
        filenode['children'] = [
            self._get_file_tree(child, user, cookie=cookie)
            for child in self._get_fileobj_child_metadata(filenode, user, **kwargs)
        ]
        return filenode


class BaseOAuthNodeSettings(BaseNodeSettings):
    # TODO: Validate this field to be sure it matches the provider's short_name
    # NOTE: Do not set this field directly. Use ``set_auth()``
    external_account = models.ForeignKey(
        ExternalAccount,
        null=True,
        blank=True,
        related_name='%(app_label)s_node_settings',
        on_delete=models.CASCADE,
    )

    # NOTE: Do not set this field directly. Use ``set_auth()``
    # user_settings = fields.AbstractForeignField()

    # The existence of this property is used to determine whether or not
    #   an addon instance is an "OAuth addon" in
    #   AddonModelMixin.get_oauth_addons().
    oauth_provider = None

    class Meta:
        abstract = True

    @abc.abstractproperty
    def folder_id(self):
        raise NotImplementedError(
            "BaseOAuthNodeSettings subclasses must expose a 'folder_id' property."
        )

    @abc.abstractproperty
    def folder_name(self):
        raise NotImplementedError(
            "BaseOAuthNodeSettings subclasses must expose a 'folder_name' property."
        )

    @abc.abstractproperty
    def folder_path(self):
        raise NotImplementedError(
            "BaseOAuthNodeSettings subclasses must expose a 'folder_path' property."
        )

    def fetch_folder_name(self):
        return self.folder_name

    @property
    def nodelogger(self):
        auth = None
        if self.user_settings:
            auth = Auth(self.user_settings.owner)
        self._logger_class = getattr(
            self,
            '_logger_class',
            type(
                '{0}NodeLogger'.format(self.config.short_name.capitalize()),
                (logger.AddonNodeLogger,),
                {'addon_short_name': self.config.short_name},
            ),
        )
        return self._logger_class(node=self.owner, auth=auth)

    @property
    def complete(self):
        return bool(
            self.has_auth
            and self.external_account
            and self.user_settings.verify_oauth_access(
                node=self.owner,
                external_account=self.external_account,
            )
        )

    @property
    def configured(self):
        return bool(
            self.complete and (self.folder_id or self.folder_name or self.folder_path)
        )

    @property
    def has_auth(self):
        """Instance has an external account and *active* permission to use it"""
        return bool(self.user_settings and self.user_settings.has_auth) and bool(
            self.external_account
            and self.user_settings.verify_oauth_access(
                node=self.owner, external_account=self.external_account
            )
        )

    def clear_settings(self):
        raise NotImplementedError(
            "BaseOAuthNodeSettings subclasses must expose a 'clear_settings' method."
        )

    def set_auth(self, external_account, user, metadata=None, log=True):
        """Connect the node addon to a user's external account.

        This method also adds the permission to use the account in the user's
        addon settings.
        """
        # tell the user's addon settings that this node is connected to it
        user_settings = user.get_or_add_addon(self.oauth_provider.short_name)
        user_settings.grant_oauth_access(
            node=self.owner,
            external_account=external_account,
            metadata=metadata,  # metadata can be passed in when forking
        )
        user_settings.save()

        # update this instance
        self.user_settings = user_settings
        self.external_account = external_account

        if log:
            self.nodelogger.log(action='node_authorized', save=True)
        self.save()

    def deauthorize(self, auth=None, add_log=False):
        """Remove authorization from this node.

        This method should be overridden for addon-specific behavior,
        such as logging and clearing non-generalizable settings.
        """
        self.clear_auth()

    def clear_auth(self):
        """Disconnect the node settings from the user settings.

        This method does not remove the node's permission in the user's addon
        settings.
        """
        self.external_account = None
        self.user_settings = None
        self.save()

    def before_remove_contributor_message(self, node, removed):
        """If contributor to be removed authorized this addon, warn that removing
        will remove addon authorization.
        """
        if self.has_auth and self.user_settings.owner == removed:
            return (
                u'The {addon} add-on for this {category} is authenticated by {name}. '
                u'Removing this user will also remove write access to {addon} '
                u'unless another contributor re-authenticates the add-on.'
            ).format(
                addon=self.config.full_name,
                category=node.project_or_component,
                name=removed.fullname,
            )

    # backwards compatibility
    before_remove_contributor = before_remove_contributor_message

    def after_remove_contributor(self, node, removed, auth=None):
        """If removed contributor authorized this addon, remove addon authorization
        from owner.
        """
        if self.user_settings and self.user_settings.owner == removed:
            # Delete OAuth tokens
            self.user_settings.oauth_grants[self.owner._id].pop(
                self.external_account._id
            )
            self.user_settings.save()
            self.clear_auth()
            message = (
                u'Because the {addon} add-on for {category} "{title}" was '
                u'authenticated by {user}, authentication information has been deleted.'
            ).format(
                addon=self.config.full_name,
                category=markupsafe.escape(node.category_display),
                title=markupsafe.escape(node.title),
                user=markupsafe.escape(removed.fullname),
            )

            if not auth or auth.user != removed:
                url = node.web_url_for('node_addons')
                message += (
                    u' You can re-authenticate on the <u><a href="{url}">add-ons</a>'
                    u'</u> page.'
                ).format(url=url)
            #
            return message

    def after_fork(self, node, fork, user, save=True):
        """After forking, copy user settings if the user is the one who authorized
        the addon.

        :return: the cloned settings
        """
        clone = super(BaseOAuthNodeSettings, self).after_fork(
            node=node,
            fork=fork,
            user=user,
            save=False,
        )
        if self.has_auth and self.user_settings.owner == user:
            metadata = None
            if self.complete:
                try:
                    metadata = self.user_settings.oauth_grants[node._id][
                        self.external_account._id
                    ]
                except (KeyError, AttributeError):
                    pass
            clone.set_auth(self.external_account, user, metadata=metadata, log=False)
        else:
            clone.clear_settings()
        if save:
            clone.save()
        return clone

    def before_register_message(self, node, user):
        """Return warning text to display if user auth will be copied to a
        registration.
        """
        if self.has_auth:
            return (
                u'The contents of {addon} add-ons cannot be registered at this time; '
                u'the {addon} add-on linked to this {category} will not be included '
                u'as part of this registration.'
            ).format(
                addon=self.config.full_name,
                category=node.project_or_component,
            )

    # backwards compatibility
    before_register = before_register_message

    def serialize_waterbutler_credentials(self):
        raise NotImplementedError(
            "BaseOAuthNodeSettings subclasses must implement a \
            'serialize_waterbutler_credentials' method."
        )

    def serialize_waterbutler_settings(self):
        raise NotImplementedError(
            "BaseOAuthNodeSettings subclasses must implement a \
            'serialize_waterbutler_settings' method."
        )


class UserSettings(BaseOAuthUserSettings):
    """Stores user-specific box information"""

    oauth_provider = Provider
    serializer = charon_serializer.BoxSerializer

    def revoke_remote_oauth_access(self, external_account):
        try:
            # TODO: write client for box, stop using third-party lib
            requests.request(
                'POST',
                charon_settings.BOX_OAUTH_REVOKE_ENDPOINT,
                params={
                    'client_id': charon_settings.BOX_KEY,
                    'client_secret': charon_settings.BOX_SECRET,
                    'token': external_account.oauth_key,
                },
            )
        except requests.HTTPError:
            pass


class NodeSettings(BaseOAuthNodeSettings, BaseStorageAddon):
    oauth_provider = Provider
    serializer = charon_serializer.BoxSerializer

    folder_id = models.TextField(null=True, blank=True)
    folder_name = models.TextField(null=True, blank=True)
    folder_path = models.TextField(null=True, blank=True)
    user_settings = models.ForeignKey(
        UserSettings, null=True, blank=True, on_delete=models.CASCADE
    )

    _api = None

    @property
    def api(self):
        """authenticated ExternalProvider instance"""
        if self._api is None:
            self._api = Provider(self.external_account)
        return self._api

    @property
    def display_name(self):
        return '{0}: {1}'.format(self.config.full_name, self.folder_id)

    def fetch_full_folder_path(self):
        return self.folder_path

    def get_folders(self, **kwargs):
        folder_id = kwargs.get('folder_id')
        if folder_id is None:
            return [
                {
                    'id': '0',
                    'path': '/',
                    'addon': 'box',
                    'kind': 'folder',
                    'name': '/ (Full Box)',
                    'urls': {
                        # 'folders': node.api_url_for('box_folder_list', folderId=0),
                        'folders': charon_serializer.api_v2_url(
                            'nodes/{}/addons/box/folders/'.format(self.owner._id),
                            params={'id': '0'},
                        )
                    },
                }
            ]

        try:
            Provider(self.external_account).refresh_oauth_key()
            oauth = OAuth2(
                client_id=charon_settings.BOX_KEY,
                client_secret=charon_settings.BOX_SECRET,
                access_token=ensure_str(self.external_account.oauth_key),
            )
            client = Client(oauth)
        except BoxAPIException:
            raise HttpResponseForbidden()

        try:
            metadata = client.folder(folder_id).get()
        except BoxAPIException:
            raise HttpResponseNotFound()
        except MaxRetryError:
            raise HttpResponseBadRequest()

        folder_path = '/'.join(
            [x['name'] for x in metadata['path_collection']['entries']]
            + [metadata['name']]
        )

        return [
            {
                'addon': 'box',
                'kind': 'folder',
                'id': item['id'],
                'name': item['name'],
                'path': os.path.join(folder_path, item['name']).replace(
                    'All Files', ''
                ),
                'urls': {
                    'folders': charon_serializer.api_v2_url(
                        'nodes/{}/addons/box/folders/'.format(self.owner._id),
                        params={'id': item['id']},
                    )
                },
            }
            for item in metadata['item_collection']['entries']
            if item['type'] == 'folder'
        ]

    def set_folder(self, folder_id, auth):
        self.folder_id = str(folder_id)
        self.folder_name, self.folder_path = self._folder_data(folder_id)
        self.nodelogger.log(action='folder_selected', save=True)

    def _folder_data(self, folder_id):
        # Split out from set_folder for ease of testing, due to
        # outgoing requests. Should only be called by set_folder
        try:
            Provider(self.external_account).refresh_oauth_key(force=True)
        except InvalidGrantError:
            raise charon_utils.exceptions.InvalidAuthError()
        try:
            oauth = OAuth2(
                client_id=charon_settings.BOX_KEY,
                client_secret=charon_settings.BOX_SECRET,
                access_token=ensure_str(self.external_account.oauth_key),
            )
            client = Client(oauth)
            folder_data = client.folder(self.folder_id).get()
        except BoxAPIException:
            raise charon_utils.exceptions.InvalidFolderError()

        folder_name = folder_data['name'].replace('All Files', '') or '/ (Full Box)'
        folder_path = (
            '/'.join(
                [
                    x['name']
                    for x in folder_data['path_collection']['entries']
                    if x['name']
                ]
                + [folder_data['name']]
            ).replace('All Files', '')
            or '/'
        )

        return folder_name, folder_path

    def clear_settings(self):
        self.folder_id = None
        self.folder_name = None
        self.folder_path = None

    def deauthorize(self, auth=None, add_log=True):
        """Remove user authorization from this node and log the event."""
        folder_id = self.folder_id
        self.clear_settings()

        if add_log:
            extra = {'folder_id': folder_id}
            self.nodelogger.log(action='node_deauthorized', extra=extra, save=True)

        self.clear_auth()

    def serialize_waterbutler_credentials(self):
        if not self.has_auth:
            raise charon_utils.exceptions.AddonError('Addon is not authorized')
        try:
            Provider(self.external_account).refresh_oauth_key()
            return {'token': self.external_account.oauth_key}
        except BoxAPIException as error:
            raise HttpResponse(error.message, status=error.status_code)

    def serialize_waterbutler_settings(self):
        if self.folder_id is None:
            raise charon_utils.exceptions.AddonError('Folder is not configured')
        return {'folder': self.folder_id}

    def create_waterbutler_log(self, auth, action, metadata):
        self.owner.add_log(
            'box_{0}'.format(action),
            auth=auth,
            params={
                'path': metadata['materialized'],
                'project': self.owner.parent_id,
                'node': self.owner._id,
                'folder': self.folder_id,
                'urls': {
                    'view': self.owner.web_url_for(
                        'addon_view_or_download_file',
                        provider='box',
                        action='view',
                        path=metadata['path'],
                    ),
                    'download': self.owner.web_url_for(
                        'addon_view_or_download_file',
                        provider='box',
                        action='download',
                        path=metadata['path'],
                    ),
                },
            },
        )

    # #### Callback overrides #####
    def after_delete(self, user=None):
        self.deauthorize(Auth(user=user), add_log=True)
        self.save()

    def on_delete(self):
        self.deauthorize(add_log=False)
        self.save()



####
            state = request.args.get('state')

            # make sure this is the same user that started the flow
            if cached_credentials.get('state') != state:
                raise PermissionsError('Request token does not match')

            try:
                # Quirk: Similarly to the `oauth2/authorize` endpoint, the `oauth2/access_token`
                #        endpoint of Bitbucket would fail if a not-none or non-empty `redirect_uri`
                #        were provided in the body of the POST request.
                if self.short_name in ADDONS_OAUTH_NO_REDIRECT:
                    redirect_uri = None
                else:
                    redirect_uri = web_url_for(
                        'oauth_callback',
                        service_name=self.short_name,
                        _absolute=True
                    )
                response = OAuth2Session(
                    self.client_id,
                    redirect_uri=redirect_uri,
                ).fetch_token(
                    self.callback_url,
                    client_secret=self.client_secret,
                    code=request.args.get('code'),
                )
            except (MissingTokenError, RequestsHTTPError):
                raise HTTPError(http_status.HTTP_503_SERVICE_UNAVAILABLE)
        # pre-set as many values as possible for the ``ExternalAccount``
        info = self._default_handle_callback(response)
        # call the hook for subclasses to parse values from the response
        info.update(self.handle_callback(response))

        return self._set_external_account(user, info)

    def _set_external_account(self, user, info):
        current_session = get_session()
        self.account, created = ExternalAccount.objects.get_or_create(
            provider=self.short_name,
            provider_id=info['provider_id'],
        )

        # ensure that provider_name is correct
        self.account.provider_name = self.name
        # required
        self.account.oauth_key = info['key']

        # only for OAuth1
        self.account.oauth_secret = info.get('secret')

        # only for OAuth2
        self.account.expires_at = info.get('expires_at')
        self.account.refresh_token = info.get('refresh_token')
        self.account.date_last_refreshed = timezone.now()

        # additional information
        self.account.display_name = info.get('display_name')
        self.account.profile_url = info.get('profile_url')

        self.account.save()

        # add it to the user's list of ``ExternalAccounts``
        if not user.external_accounts.filter(id=self.account.id).exists():
            user.external_accounts.add(self.account)
            user.save()

        if self.short_name in current_session.get('oauth_states', {}):
            del current_session['oauth_states'][self.short_name]
            current_session.save()

        return True

    def _default_handle_callback(self, data):
        """Parse as much out of the key exchange's response as possible.

        This should not be over-ridden in subclasses.
        """
        if self._oauth_version == OAUTH1:
            key = data.get('oauth_token')
            secret = data.get('oauth_token_secret')

            values = {}

            if key:
                values['key'] = key
            if secret:
                values['secret'] = secret

            return values

        elif self._oauth_version == OAUTH2:
            key = data.get('access_token')
            refresh_token = data.get('refresh_token')
            expires_at = data.get('expires_at')
            scopes = data.get('scope')

            values = {}

            if key:
                values['key'] = key
            if scopes:
                values['scope'] = scopes
            if refresh_token:
                values['refresh_token'] = refresh_token
            if expires_at:
                values['expires_at'] = dt.datetime.fromtimestamp(
                    float(expires_at)
                )

            return values