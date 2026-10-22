FROM alpine:3.20
RUN apk add --no-cache ffmpeg python3
COPY relay.py /relay.py
USER 1000:1000
EXPOSE 8080
CMD ["python3", "-u", "/relay.py"]
