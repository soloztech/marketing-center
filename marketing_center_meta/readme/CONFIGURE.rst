Create a Meta reader profile under Marketing Center / Configuration. Store only the
environment keys or mounted secret filenames in Odoo, then validate and discover ad
accounts. The reader token must prove every configured required scope, including
``ads_read``.

For the Soloz deployment, prefer the ``file`` backend. Mount a private directory as
``/run/secrets/meta-marketing``, expose only
``ODOO_META_API_SECRET_DIR=/run/secrets/meta-marketing`` and use versioned references
such as ``soloz_meta_ads_app_secret_v1`` and
``soloz_meta_ads_reader_token_v1``. Secret files must be regular files, mode ``0600``
and mounted read-only. Do not reuse a Messenger/Instagram Page token as an Ads reader.

After discovery, open a Meta Ads source and choose ``Sync Meta catalog``. The action
creates one provider-neutral run and queues campaign, ad-set, ad and creative pages in
that strict order. No remote object is changed.

Upgrade ``marketing_center_base`` and ``marketing_center_meta`` together. Version
``16.0.1.1.0`` of this addon inserts its action into the source-form header extension
point introduced by ``marketing_center_base`` ``16.0.1.2.1``.
