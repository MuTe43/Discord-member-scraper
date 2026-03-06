# Contributing to Server Lens

Thanks for your interest in contributing! Here's how to get started.

## Development Setup

```bash
# Clone the repo
git clone https://github.com/MuTe43/Discord-member-scraper.git
cd server-lens

# Install dependencies
pip install -r requirements.txt

# Run the dev server
cd backend
python main.py
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

## Project Structure

```
server-lens/
├── backend/
│   ├── main.py        # FastAPI routes + app setup
│   ├── gateway.py     # Discord Gateway session management
│   ├── scraper.py     # Member scraping logic + REST helpers
│   └── models.py      # Pydantic request/response models
├── frontend/
│   ├── index.html     # Page structure
│   ├── style.css      # All styles
│   └── app.js         # Application logic
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## How to Contribute

1. **Fork** the repository
2. **Create a branch** for your feature: `git checkout -b feature/my-feature`
3. **Make your changes** and test them locally
4. **Commit** with a clear message: `git commit -m "Add: my new feature"`
5. **Push** and open a **Pull Request**

## Guidelines

- Keep PRs focused — one feature or fix per PR
- Test your changes locally before submitting
- Follow the existing code style
- Update the README if you add new features

## Reporting Bugs

Open an [issue](../../issues) with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Your OS and Python version

## Feature Requests

Open an issue with the `enhancement` label describing the feature and why it would be useful.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
