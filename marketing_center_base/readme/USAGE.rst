Provider and bridge addons create touchpoints through
``marketing.attribution.service._ingest_touchpoint``.  The leading underscore keeps
the Python service outside the public RPC surface. Ledger models reject direct
create, write and unlink operations, including from administrators.
