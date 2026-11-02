# Secret-file access policy

The engine denies retrieval, read-tool access, and worker mutations for known
credential, key, state, and secret paths through `jev.retrieval.is_secret_path`.
This includes secret directories, credential manifests, package registries,
VCS/container credential files, Terraform state/variable files, private-key
names, and common keystore formats.

`docs/secrets-policy.md` is intentionally **not** itself a secret path. It is
public policy documentation and contains no credentials. The denylist test
`test_normal_paths_and_documented_policy_are_allowed` records this decision.
