FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
COPY .git/HEAD ./.git/HEAD
COPY .git/refs ./.git/refs
ARG BRIDGE_BUILD_COMMIT=""
ARG BRIDGE_BUILD_REF=""
ARG BRIDGE_BUILD_SOURCE=""
RUN set -eu; \
    commit="$BRIDGE_BUILD_COMMIT"; \
    ref="$BRIDGE_BUILD_REF"; \
    source="$BRIDGE_BUILD_SOURCE"; \
    if [ -z "$commit" ] && [ -f .git/HEAD ]; then \
      head="$(cat .git/HEAD)"; \
      if [ "${head#ref: }" != "$head" ]; then \
        git_ref="${head#ref: }"; \
        ref="${ref:-${git_ref#refs/heads/}}"; \
        if [ -f ".git/$git_ref" ]; then \
          commit="$(cat ".git/$git_ref")"; \
        fi; \
      else \
        commit="$head"; \
        ref="${ref:-detached}"; \
      fi; \
      source="${source:-git}"; \
    fi; \
    commit="${commit:-unknown}"; \
    ref="${ref:-unknown}"; \
    source="${source:-unknown}"; \
    BRIDGE_COMMIT="$commit" BRIDGE_REF="$ref" BRIDGE_SOURCE="$source" \
      python -c 'import json, os; open("app/_build.json", "w", encoding="utf-8").write(json.dumps({"commit": os.environ["BRIDGE_COMMIT"], "ref": os.environ["BRIDGE_REF"], "source": os.environ["BRIDGE_SOURCE"]}) + "\n")'; \
    rm -rf .git
RUN pip install --no-cache-dir .

EXPOSE 8090
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
