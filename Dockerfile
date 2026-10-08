# Image for the stub; see README.md "Running it".
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .

# The local oracle, run inside the image it will ship in: a failing test
# (including the single-segment socket check) fails the build.
FROM base AS test
RUN pip install --no-cache-dir pytest
COPY test_server.py .
RUN python -m pytest -q test_server.py && touch /tests-passed

FROM base
# Forces the test stage to run; BuildKit skips stages nothing depends on.
COPY --from=test /tests-passed /tests-passed
USER nobody
ENTRYPOINT ["python", "server.py"]
CMD ["--port", "8080"]
