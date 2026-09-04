Create the shared ``meta.api.app`` identity first, then select it on a Meta reader
profile under Marketing Center / Configuration. The shared App owns the App ID,
Graph version and App Secret reference. The reader profile owns only its Ads access
token backend/reference and required scopes. Validate the profile, then discover ad
accounts. The reader token must prove every configured required scope, including
``ads_read``.

For the Soloz deployment, prefer the ``file`` backend. Mount a private directory as
``/run/secrets/meta-marketing``, expose only
``ODOO_META_API_SECRET_DIR=/run/secrets/meta-marketing`` and use versioned references
such as ``soloz_meta_app_secret_v1`` on the shared App and
``soloz_meta_ads_reader_token_v1`` on the reader profile. Secret files must be regular files, mode ``0600``
and mounted read-only. Do not reuse a Messenger/Instagram Page token as an Ads reader.

After discovery, open a Meta Ads source and choose ``Sync Meta catalog``. The action
creates one provider-neutral run and queues campaign, ad-set, ad and creative pages in
that strict order. No remote object is changed.

Upgrade ``marketing_center_base`` and ``marketing_center_meta`` in the same
maintenance window. The connector extends the source-form contract owned by the
base addon, so deploying only one side can leave the registry with incompatible
views or services.
