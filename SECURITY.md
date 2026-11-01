# Security

If you find a security problem in iOS Studio, please report it privately. Open the [Security tab](https://github.com/ArtMin96/omarchy-ios-studio/security) on GitHub and click "Report a vulnerability". Only the maintainer sees the report. Please don't open a public issue for it.

Include what you found, how to reproduce it, and which version or commit you used. I'll reply as soon as I can and keep you posted while it gets fixed.

## What's in scope

Everything in this repository: the app, the helper (`bin/iosstudio-helper`), the device daemon (`bin/iosstudio-deviced`), the MCP server (`bin/iosstudio-mcp`) and `install.sh`. I care most about bugs that could:

- leak Apple ID credentials, 2FA codes, signing keys or certificates
- let something install, launch or remove apps, change settings or pair with an iPhone without the owner's consent
- run commands or write files outside what a feature is meant to do, including through an AI agent connected to the MCP server

Bugs in xtool, pymobiledevice3, omarchy-apple-dev or Omarchy itself belong in those projects, though a heads-up here is welcome if iOS Studio exposes them.

## Bounties

iOS Studio is a free open source project, so there are no bug bounties. I'm happy to credit you in the fix if you'd like.
