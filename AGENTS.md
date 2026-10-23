# Working on PHP Package Builder

- Profiles are the source of truth for versions, target platforms, source hashes
  and selected checks. Keep one explicit target per profile.
- The only existing target is experimental. Claim a successful full build or
  supported platform only with actual build evidence; record evidence in
  docs/verification before marking a profile verified.
- Host-side code uses Python 3.8+ and the standard library. Compilation and APT
  installation belong inside Docker, not on the operator's host.
- Keep APT signature verification and pinned source checksums. Do not fix
  dependency failures with --force-depends, --allow-unauthenticated or trusted=yes.
- Package artifacts, downloaded archives, caches, logs and credentials do not
  belong in Git. Preserve upstream copyright/license information in packages.
- Test meaningful invariants: input integrity, target matching, version order,
  failure propagation and container cleanup. Run:
  python3 -m unittest discover -s tests -v
  ./bin/php-build check
- When Docker is available, run the affected full profile. When unavailable,
  state exactly which checks ran and which remain unverified.
- Installation on an application server is a separate operator task, requiring
  target-specific dependency and application checks. Do not add implicit host
  upgrades or service restarts to the build command.
