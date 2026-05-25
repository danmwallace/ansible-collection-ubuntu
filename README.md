# Ansible Collection — danmwallace.ubuntu

Ubuntu-specific homelab configuration for Dan's automation workspace. This
collection holds the Ubuntu-only roles that don't belong in the cross-distro
[`danmwallace.linux`](https://github.com/danmwallace/ansible-collection-linux)
collection — things that lean on Ubuntu/Debian package management, APT
repositories, or Ubuntu-specific defaults.

It is **not published to Ansible Galaxy**. The first release is deferred until
the collection actually has roles, so install it from the git source rather
than via `ansible-galaxy collection install`.

## Status

**Scaffolding only — no roles yet.** The `roles/` directory is empty and the
first publish is intentionally deferred (see `CHANGELOG.md`). Roles are
forthcoming; this README will gain a role index once they land.

## Requirements

- Ansible >= 2.16 (from `meta/runtime.yml`)

This collection declares no other collection dependencies (`galaxy.yml`
`dependencies: {}`).

## Installation

Not on Galaxy. Install from source or pin the git repository in your
`requirements.yml`:

```yaml
collections:
  - name: https://github.com/danmwallace/ansible-collection-ubuntu.git
    type: git
    version: main
```

## License

MIT
