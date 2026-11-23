# Contributing

Thanks for helping. A few rules keep this project safe to maintain.

## Hard boundary

chatwarden reads **plain, already-readable** message stores and nothing else.
We do not accept, in code, docs, issues or comments:

- decryption of any chat client's database or media, key extraction, memory
  scanning, or reverse-engineered private protocols;
- names of, or links to, tools that do those things;
- real account ids, chat names, message logs or other personal data.

Contributions that cross this line are closed and removed.

## Developer Certificate of Origin

There is no CLA. Instead every commit must carry a sign-off line certifying
the [Developer Certificate of Origin 1.1](https://developercertificate.org/):

    Signed-off-by: Your Name <you@example.com>

`git commit -s` adds it for you.

## Tests

    python3 -m unittest tests.test_gateway
    sh tests/run_smokes.sh
    sh tests/e2e_demo.sh        # inside the container

All tests run offline. New behaviour needs a smoke test next to the others in
`bot/`.

## Style

The code came out of a long-running deployment, and many historical names
(`_vNNN_` methods, `v3194_*` tables, `[v3.30-...]` log tags) are referenced by
tests and ops tooling; keep them. Prefer small, local changes over sweeping
refactors, and keep new integration points behind the existing interfaces
described in `docs/ARCHITECTURE.md`. Coding agents should read `AGENTS.md`.

## Security

Please don't report vulnerabilities in public issues; see `SECURITY.md`.

## License

By contributing you agree that your work is licensed under AGPL-3.0-only.
