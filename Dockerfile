# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS upstream
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/base.tar.gz
RUN mkdir /base && tar xzf /tmp/base.tar.gz -C /base --strip-components=1

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY --from=upstream /base /app/base
COPY maps/coverage.json.gz maps/positions.json /app/maps/
COPY selector.py scheduler.py impact.py trivia.py /app/
CMD ["python", "/app/selector.py"]
