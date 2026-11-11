# Contributing to streamline

Thanks for your interest. This is a small personal project, so please keep changes focused.

## Reporting bugs and asking for features

Open an [issue](https://github.com/abhichandra21/streamline/issues).
For bugs, include what you did, what you expected, what happened instead, and your OS and version.

For security problems, do not open a public issue. See [SECURITY.md](SECURITY.md).

## Making changes

1. Open an issue first for anything larger than a small fix, so we can agree on the approach.
2. Fork the repo and create a branch for your change.
3. Make the change and run the tests (see below).
4. Open a pull request that explains what changed and why.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 -m pytest -q
```

See the [roadmap](https://streamline-docs.pages.dev/reference/roadmap/) for what's planned.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
