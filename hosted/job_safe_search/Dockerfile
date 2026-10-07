FROM python:3.12-slim
WORKDIR /app
COPY hosted/job_safe_search/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY hosted/job_safe_search /app/hosted/job_safe_search
RUN useradd --uid 10001 --create-home search && mkdir /data && chown search /data
USER search
EXPOSE 8422
CMD ["python", "-m", "hosted.job_safe_search.service"]
