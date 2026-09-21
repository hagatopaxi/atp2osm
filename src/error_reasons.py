"""Quick-pick reasons for invalidating an ATP suggestion.

Keys follow a `<subject>_<problem>` structure — subject first so the reasons
group by the field they blame, problem second so the same wording means the
same thing across subjects. They are what gets stored in
`import_history.comment`, so a label change never rewrites history. The labels
themselves live in `website/templates/_error_reasons.html`, where a translated
string can be resolved inside a request. Derived from the free-text comments of
the first 131 rejections in production.
"""

ERROR_REASONS = (
    "website_generic",
    "website_broken",
    "website_language",
    "phone_wrong",
    "phone_format",
    "opening_hours_wrong",
    "address_wrong",
    "email_generic",
    "poi_mismatch",
    "data_unverifiable",
    # Not a POI of its own: the whole batch was turned down, type by type.
    "types_all_excluded",
    # No longer offered: the comment field stands on its own. Kept so that the
    # rejections recorded with it keep their label.
    "other",
)
