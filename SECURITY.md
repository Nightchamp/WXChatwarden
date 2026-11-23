# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub: open the
repository's **Security** tab and choose **Report a vulnerability**. Do not
open a public issue, PR or discussion for it.

Include what you found, how to reproduce it, and the impact you expect. Leave
out real chat data, account ids and secrets; invented examples are enough.

We aim to acknowledge a report within a week and will keep you updated until
it is fixed or declined.

## Scope

In scope: the bot (`bot/`), the gateway (`gateway/`), the tools in `tools/`,
and the Docker and Compose files in this repository, for example a way to
bypass the owner check, make the gateway reachable without its bearer key,
read files outside the configured stores, or leak secrets into logs.

Out of scope: third-party chat clients, model providers and container images
this project talks to, and deployments that changed the defaults in ways this
repository does not ship.

This project does not accept, and will not discuss, decryption of chat client
data or key extraction; see `CONTRIBUTING.md`.
