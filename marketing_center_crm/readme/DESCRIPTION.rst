Compatibility package for installations predating Marketing Center 16.0.2.0.0.
The business implementation, views, permissions and tests are owned by
``marketing_center_base``. Existing XML IDs and Python imports remain compatible.

New installations should select ``marketing_center_base``. Existing installations
must upgrade the core together with this package. Do not uninstall this package
as part of the fusion. See ``docs/core-fusion.md``.
