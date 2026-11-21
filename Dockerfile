# The scorer builds this on linux/amd64 and runs it with no network.
# Same base image the track labeled with: its Python decides how sqlglot's tests behave.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
# The track's exact test environment, so tests run here behave as they did when labeled.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
# Pinned sqlglot sources and tests: patches are applied to a scratch copy of these.
RUN python -c "import urllib.request,tarfile,io,os; \
data=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz').read(); \
tarfile.open(fileobj=io.BytesIO(data)).extractall('/tmp/x', filter='data'); \
os.rename('/tmp/x/sqlglot-${SQLGLOT_SHA}', '/sqlglot')" \
 && rm -rf /sqlglot/sqlglot-integration-tests /sqlglot/docs /sqlglot/posts /sqlglot/benchmarks /sqlglot/pdoc \
 && python -m compileall -q /usr/local/lib/python3.13 /sqlglot/sqlglot /sqlglot/tests > /dev/null; \
 chmod -R a+rX /sqlglot
COPY selector.py verify.py replay.py covmap.json.gz replay_ref.json runtime_lines.json pickle_lines.json testcost.json attrmap.json.gz /app/
COPY opmap /app/opmap
RUN BR_SRC=/sqlglot python /app/selector.py --build-text-index /app/textindex.pkl.gz && python -m compileall -q /app && chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1 SKIP_INTEGRATION=0
CMD ["python", "/app/selector.py"]
