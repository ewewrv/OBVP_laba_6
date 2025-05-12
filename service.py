import asyncio
import aio_pika
import logging
from tenacity import retry, wait_fixed, stop_after_attempt
import json
import time
from prometheus_client import start_http_server, Counter, Histogram
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import format_trace_id

# Настройка трейсинга
trace.set_tracer_provider(
    TracerProvider(
        resource=Resource.create({"service.name": "message-service"})
    )
)
otlp_exporter = OTLPSpanExporter(endpoint="http://tempo:4317", insecure=True)
trace.get_tracer_provider().add_span_processor(
    BatchSpanProcessor(otlp_exporter)
)
tracer = trace.get_tracer(__name__)

# Метрики Prometheus
MESSAGES_PROCESSED = Counter(
    "messages_processed_total", 
    "Total number of messages processed"
)
MESSAGES_ERRORS = Counter(
    "messages_processing_errors_total", 
    "Total number of processing errors"
)
MESSAGE_PROCESSING_TIME = Histogram(
    "message_processing_duration_seconds", 
    "Time spent processing message",
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5]
)

# Настройки
RABBITMQ_URL = "amqp://admin:admin@rabbitmq:5672/"
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@retry(wait=wait_fixed(2), stop=stop_after_attempt(5))
async def connect_to_rabbitmq():
    logger.info("Connecting to RabbitMQ...")
    return await aio_pika.connect_robust(RABBITMQ_URL)

async def main():
    # Запуск сервера метрик
    start_http_server(8001)
    logger.info("Prometheus metrics server started on port 8001")
    
    connection = None
    try:
        connection = await connect_to_rabbitmq()
        channel = await connection.channel()
        exchange = await channel.declare_exchange(
            "messages", aio_pika.ExchangeType.DIRECT
        )
        queue = await channel.declare_queue("service_queue", durable=True)
        await queue.bind(exchange, routing_key="service_queue")

        logger.info("Service started. Waiting for messages...")

        async with queue.iterator() as queue_iter:
            async for message in queue_iter:
                start_time = time.time()
                try:
                    incoming_data = json.loads(message.body)
                    trace_id = incoming_data.get("trace_id")
                    incoming_message = incoming_data.get("message")

                    with tracer.start_as_current_span("process_message") as span:
                        span.set_attribute("custom.trace_id", trace_id)
                        logger.info(f"[TRACE_ID: {trace_id}] Processing: {incoming_message}")
                        
                        # Обработка сообщения
                        response_text = f"Processed: {incoming_message.upper()}"
                        response_payload = {
                            "trace_id": trace_id,
                            "result": response_text
                        }

                        if message.reply_to:
                            await channel.default_exchange.publish(
                                aio_pika.Message(
                                    body=json.dumps(response_payload).encode(),
                                    correlation_id=message.correlation_id
                                ),
                                routing_key=message.reply_to
                            )

                    MESSAGES_PROCESSED.inc()

                except Exception as e:
                    MESSAGES_ERRORS.inc()
                    logger.error(f"Message processing failed: {e}")
                    await message.nack()
                finally:
                    MESSAGE_PROCESSING_TIME.observe(time.time() - start_time)
                    await message.ack()

    except Exception as e:
        logger.error(f"Service error: {e}")
    finally:
        if connection:
            await connection.close()

if __name__ == "__main__":
    asyncio.run(main())

# © Кравченко