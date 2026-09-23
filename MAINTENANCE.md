# Profile maintenance

The profile uses Markdown and repository-hosted SVGs. The artwork has light and dark variants and does not depend on a third-party stats-image service.

## Edit the profile

- Edit personal copy, links, and the toolbox in `README.md`.
- Change project selection, descriptions, and artwork in `scripts/update_profile.py`.
- Keep the two metrics markers in the README. Only the text between those markers is generated.
- Do not add a resume, personal contact details, employer names, or employment metrics to this repository.

To regenerate artwork from the checked-in snapshot without network access:

```sh
python3 scripts/update_profile.py --from-cache
python3 -m unittest discover -s tests -v
```

The renderer uses only the Python standard library. Python 3.12 is used in CI.

## Refresh the numbers

The Profile metrics workflow runs daily at 13:23 UTC and can also be started manually from Actions. It uses the repository's built-in `GITHUB_TOKEN`; no personal token or additional secret is required. The refresh job only writes on this repository's default branch.

To fetch current public data locally, set `GITHUB_TOKEN` or `GH_TOKEN` in the environment and run:

```sh
python3 scripts/update_profile.py
```

The script paginates the public repository list, excludes private repositories and forks from star/fork totals, and fetches download counts for every package in the Packagist namespace. The token is sent only to GitHub. API failures stop generation and preserve the existing published assets. The snapshot records a UTC date and exact counts. Downloads can include repeated installs and maintained forks; they do not measure people.

Language shares use primary-language repository counts, not lines of code or proficiency. Archived original repositories are included. Repositories without a detected language are excluded from the language denominator.

All API requests must succeed before output is written. Packagist requests are limited to four concurrent requests. Failed workflow runs are visible in Actions. GitHub can disable scheduled workflows in inactive public repositories; if updates stop, check Actions and re-enable the schedule.

## Review before publishing

Check the profile at desktop and phone widths in both light and dark modes. All project cards should link to their matching public repository. Confirm the README has no broken local asset references and that generated assets match the snapshot.

GitHub profile content comes from `README.md` on the default branch of `jeremykenedy/jeremykenedy`. Merging a profile change into `main` publishes it to the profile.
