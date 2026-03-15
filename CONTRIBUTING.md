# Contributing to microsoft-teams-cli

Thanks for your interest in contributing!

## Getting Started

```sh
git clone https://github.com/yusufaltunbicak/microsoft-teams-cli.git
cd microsoft-teams-cli
pip install -e ".[test]"
playwright install chromium
```

## Development

- Python 3.10+
- No linter configured
- Follow existing code style (type hints, Click commands, Rich for stderr)

## Submitting Changes

1. Fork the repo and create a feature branch
2. Make your changes
3. Run `pytest` to make sure tests pass
4. Test manually with `teams` commands
5. Submit a pull request with a clear description

## Reporting Issues

- Use [GitHub Issues](https://github.com/yusufaltunbicak/microsoft-teams-cli/issues)
- Include your Python version, OS, and steps to reproduce

## Code Style

- Type hints for function signatures
- Click for CLI commands
- Rich for terminal output (stderr), stdout reserved for JSON
- httpx for HTTP requests

## Notes

- **Do not commit tokens, credentials, or personal data**
- IC3 and UPS endpoints are reverse-engineered from the Teams web client — changes here need extra care
- All send/delete/edit operations should include confirmation prompts by default
