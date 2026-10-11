# User update B1 fixture provenance

## Primary fixture: genuine response structure with redacted values

`user_update_read_model.json` was copied byte-for-byte from the file supplied in
`Codex_B1_missing_inputs.zip`. Per the user's `README_FOR_CODEX.md`, it comes from
a historical successful `get_user_excel_form` response captured by Allure in
`wizbank_api_test`. It is **not synthetic**. No live request was made to obtain it.

The user redacted `usr_ste_usr_id`, `usr_display_bil`, `usgDisplayBil`,
`userGroupId` and `gradeId`; `usr_pwd` is `<REDACTED>`. Field names, types and
null/empty-string/empty-array states were retained. The original Allure
attachment is not included in this supplement, so provenance is attributed to
the user's supplied description, not an independently fetched response.

The primary fixture is the default input for Builder unit tests. In particular:

- `usr_ent_id` is the positive integer `1830`.
- `usr_gender` is `""`; ordinary nullable scalars retain their supplied values.
- `positionId`, `highestApprovalUserGroupId`, `subordinateDepartmentId` are null.
- `userTagVOList` is `[]`; `leaderList` is null.
- `roleCode` is `NLRN_1,TADM_1`; `ssoConnectionEnable` is `No`.
- Read Model `curLan` is null; Write Model `curLan` comes from the explicit
  Builder argument.

## Supplemental fixture: synthetic relations

`user_update_read_model_with_relations.json` remains explicitly **synthetic**.
Its metadata and `data` envelope distinguish it from the primary fixture. It
supplies Tag/Leader relationships absent from the genuine sample and verifies
order-preserving derivation. Its synthetic gender was corrected to `""` to
conform to the RFC's `M` / `F` / `""` contract.

## B1 contract and compatibility

- All 24 directly copied fields and both relation-list keys are dependencies;
  a missing key never silently becomes null.
- Positive ASCII decimal-string `usr_ent_id` values are a compatibility policy.
  The observed genuine JSON type is integer. Neither representation is coerced.
- Role tokens must be nonblank and unique (including whitespace-equivalent
  duplicates); accepted role strings keep their original order and contents.
- Nullable relation IDs preserve null; non-null empty/invalid IDs fail fast.
- An empty mutable-field set with empty overrides yields two independent,
  identical payloads, consistent with the task's subset/equality guards.

The [B1 implementation document](../../docs/v2.4.6B1_user_update_payload_builder.md)
contains the RFC B1-01 through B1-45 coverage table and complete payload examples.
No whitelist fields or runtime integration were added by this supplement.
