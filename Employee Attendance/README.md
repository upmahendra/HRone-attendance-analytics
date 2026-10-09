# Employee Attendance & Analytics API

This project implements the FastAPI employee attendance service described in the provided OpenAPI contract and data model. It covers employee creation, punch-in/punch-out flows, manual regularization with an audit trail, and the required monthly/department/leaderboard/trend analytics.

## Features
- Employee CRUD for business-key `emp_code` values
- MongoDB startup index creation for uniqueness and query efficiency
- IST-aware attendance-date and overnight-shift logic
- Derived-field calculations for late minutes, overtime, work hours, and half-day status
- Manual correction endpoint with `history` audit entries
- Analytics endpoints for employee monthly summaries, department summaries, late leaderboard, and department trend
- MongoDB `explain` endpoint for the main query shapes

## Local setup
```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# macOS/Linux
# source .venv/bin/activate

pip install -r requirements.txt

# Optional: run MongoDB locally with Docker
# docker run -d --name attendance-mongo -p 27017:27017 mongo:7

# Configure environment if needed
# export MONGO_URI="mongodb://localhost:27017"
# export MONGO_DB="attendance_db"

python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

## Verification
The app was validated with a Python compile check and contract-focused business-rule assertions for late-minute handling, overtime, overnight shifts, and work-hour rounding.

## Notes
- The repository is expected to be pushed to a public GitHub repository before submission.
- No `.env`, secrets, or Dockerfile are included in the repo.
