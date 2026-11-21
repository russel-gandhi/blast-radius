# The scorer builds this on linux/amd64 and runs it with no network.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
# Pinned sqlglot sources: the selector reads the original files to apply each patch and diff ASTs.
RUN python -c "import urllib.request,tarfile,io,os; \
data=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz').read(); \
tarfile.open(fileobj=io.BytesIO(data)).extractall('/tmp/x', filter='data'); \
os.rename('/tmp/x/sqlglot-${SQLGLOT_SHA}', '/sqlglot')" \
 && chmod -R a+rX /sqlglot
COPY selector.py covmap.json.gz /app/
RUN chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/selector.py"]
