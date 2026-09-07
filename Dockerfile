FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY ecommerce_rag /app/ecommerce_rag

CMD ["python", "-c", "from ecommerce_rag.tool_schema import TOOL_SCHEMAS; from ecommerce_rag.tools import WRITE_TOOLS; print(f'{len(TOOL_SCHEMAS)} schemas, {len(WRITE_TOOLS)} write tools')"]
