# Terraform state recovery runbook

## Data-protection status

Recovery capability depends on features that are being introduced in code but
are **not guaranteed to be live on the storage account until a `terraform apply`
has run**. Confirm the deployed state before relying on version-based recovery:

```
az storage account blob-service-properties show \
  -n <storage-account> -g <resource-group> \
  --query '{versioning:isVersioningEnabled, blobSoftDelete:deleteRetentionPolicy.enabled, containerSoftDelete:containerDeleteRetentionPolicy.enabled}' -o json
```

- If versioning and soft delete are **enabled**, the version-based sequence
  below applies.
- If they are **disabled** (the account's state prior to applying the Stage 2
  hardening), steps that reference blob versions are not available. In that
  case, recovery is limited to the forensic backup taken in step 4 and any
  external copies, and the current blob must be treated as the only source.

## Never do these first

- Do not disable state locking.
- Do not manually overwrite the current blob.
- Do not force-unlock without checking for active operations.
- Do not delete the state container.
- Do not recreate resources before inspecting state history.

## Recovery sequence

1. Stop all Terraform pipelines and local operations.
2. Confirm no active operation owns the state lease.
3. Record the current state blob version ID.
4. Download the current state for forensic backup.
5. Identify the last known good blob version.
6. Review the state difference before restoration.
7. Restore or promote the selected version.
8. Run `terraform state list`.
9. Run `terraform plan`.
10. Resume deployment only when the plan is understood.

## Deferred: bring the CanNotDelete lock under Terraform management

A CanNotDelete lock (`lock-terraform-state-delete`) currently protects the
storage account but was created out-of-band and is **not** managed by
Terraform. Managing it in code is deferred. When adopting it, do so as its own
reviewed change — do not apply it blind:

1. Add an `azurerm_management_lock` resource scoped to the storage account,
   matching the existing lock's name and level (`CanNotDelete`).
2. Find the existing lock's resource ID:

   ```
   az lock list --resource-group <resource-group> -o json
   ```

3. Import it so no new lock is created and none is destroyed:

   ```
   terraform import azurerm_management_lock.terraform_state <lock-id>
   ```

4. Run `terraform plan` and confirm it reports **no changes** for the lock
   (an import that shows a replacement means the resource arguments do not match
   the live lock — reconcile before applying).

Until this import is done, treat the lock as manually managed and do not remove
it through the portal or CLI.
