# Metadata curation review — table prototype

This standalone document is a visual sandbox for the pull-request review body.
Edit the Markdown and use GitHub's rendered preview to iterate on the layout.
It is not read by the curation workflow and its sample decisions are not
authoritative.

Review the YAML diff in **Files changed** and this compact table, then check
exactly one option for each record in the task-list controls below it.

| Record | Proposed change | Decision | Source ID | Path | Blockers |
|:--|:--|:--|:--|:--|:--|
| **Annual Review of Neuroscience**<br><code>xyzrins:publication-venues/issn-0147-006x</code> | `name`: `Annual Reviews` → `Annual Review of Neuroscience`<br>`annotations`: PAV source provenance | Choose below | <code>XYZPublicationVenue:ISSN:0147-006X</code> | <code>XYZPublicationVenue/issn-0147-006x.yaml</code> | — |
| **Austin Macdonald**<br><code>xyzrins:persons/austin-macdonald</code> | `affiliations`: `—` → `1 imported relation`<br>`annotations`: PAV source provenance | Accept unavailable | <code>XYZPerson:xyzrins:persons/austin-macdonald</code> | <code>XYZPerson/austin-macdonald.yaml</code> | unresolved relation: <https://centerforopenneuroscience.org/whoweare><br>unresolved relation: rrid:SCR_002630 |
| **Center for Open Neuroscience**<br><code>xyzrins:organizations/con</code> | `new record` | Choose below | <code>XYZOrganization:CON</code> | <code>XYZOrganization/con.yaml</code> | — |

## Decision controls

- [ ] Accept — **Annual Review of Neuroscience**
- [ ] Reject — **Annual Review of Neuroscience**
- [ ] Defer — **Annual Review of Neuroscience**

Accept is unavailable for Austin Macdonald: unresolved relation.

- [ ] Reject — **Austin Macdonald**
- [ ] Defer — **Austin Macdonald**

- [ ] Accept — **Center for Open Neuroscience**
- [ ] Reject — **Center for Open Neuroscience**
- [ ] Defer — **Center for Open Neuroscience**

The technical source ID, path, and blockers stay available to the right of the
review-focused columns without interrupting the record/change comparison.
