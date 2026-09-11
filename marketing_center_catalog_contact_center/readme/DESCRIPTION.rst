An optional bridge between ``marketing_center_catalog`` and ``contact_center_ui``.
Agents consult company information, product documents, media, links and FAQs from
the conversation composer and add shareable content to their current draft.

Searches and downloads require catalog read access and access to the selected
conversation. Content is restricted to the conversation company, even when the
agent has several active companies. Sharing rechecks availability and the editor's
shareable flag. Original binary attachments stay owned by their catalog items;
the existing authenticated download/upload flow creates conversation copies.

No public catalog URLs or editorial approval workflow are introduced. This bridge
does not install sales, attribution, advertising providers or AI modules. It is
not installed automatically by ``marketing_center_suite``.
