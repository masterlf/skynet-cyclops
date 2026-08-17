# Security policy

## TL;DR

Report vulnerabilities through GitHub's private vulnerability reporting feature. Do not include real secrets, personal data, or production evidence in public issues.

## Supported versions

Skynet-Cyclops is pre-1.0. Security fixes target the latest release and the default branch.

## Reporting a vulnerability

Use **Security → Report a vulnerability** in this repository. Include:

- affected version or commit;
- minimal synthetic reproduction;
- impact and prerequisites;
- suggested mitigation, if known.

Do not submit credentials, tokens, customer data, private infrastructure details, or exploit output from systems you do not own or lack authorization to test.

## Security boundary

Skynet-Cyclops supervises workflow metadata. It is not a sandbox, credential vault, endpoint security product, or authorization substitute. The supervisor intentionally has less authority than the workflows it observes.
