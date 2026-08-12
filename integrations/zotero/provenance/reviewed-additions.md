# Reviewed Zotero additions

The CON Zotero group is a maintained source, not an unrestricted output target.
New items must be reviewed before a maintenance command can write them.

## Review record

`inputs/zotero_centerforopenneuroscience/reviewed-additions.yaml` records the
canonical DOI, complete proposed Zotero fields, target collection, evidence,
classification rationale, reviewer, date, and status. Any developer with
repository access can propose or approve an entry through normal Git review.
Only `status: approved` entries are eligible for writing.

`External` means the work is relevant to the modeled ecosystem but does not meet
the reviewed threshold for a CON publication. It does not mean unimportant,
untrusted, or excluded from the model.

## Commands

Apply approved additions with a local API key:

```bash
pixi run zotero-add-reviewed
```

Refresh the public source snapshot:

```bash
pixi run zotero-fetch
```

Check that every approved addition is present in its reviewed collection:

```bash
pixi run zotero-reviewed-check
```

The writer resolves the collection by name, checks exact normalized DOI
duplicates, fetches Zotero's current item template, and uses a one-use write
token. It refuses to move an existing item between collections because that
would be a separate editorial decision. The API key stays outside the
repository and is never included in a request URL or command output.

The offline snapshot check is part of `zotero-check`, so CI fails if an approved
entry disappears from the refreshed snapshot or is moved to another
collection.

## References

- [Zotero Web API write requests](https://www.zotero.org/support/dev/web_api/v3/write_requests)
- [Zotero Web API authentication and versioning](https://www.zotero.org/support/dev/web_api/v3/basics)
