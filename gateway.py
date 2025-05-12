from fastapi import FastAPI, HTTPException
import aio_pika
from pydantic import BaseModel
import logging
from tenacity import retry, wait_fixed, stop_after_attempt
import uuid
import asyncio
import json
from prometheus_fastapi_instrumentator import Instrumentator
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import format_trace_id

# Настройка трейсинга
trace.set_tracer_provider(
    TracerProvider(
        resource=Resource.create({"service.name": "gateway-service"})
    )
)
otlp_exporter = OTLPSpanExporter(endpoint="http://tempo:4317", insecure=True)
trace.get_tracer_provider().add_span_processor(
    BatchSpanProcessor(otlp_exporter)
)
tracer = trace.get_tracer(__name__)

app = FastAPI()

# Настройка метрик Prometheus
Instrumentator().instrument(app).expose(app)

# Настройки
RABBITMQ_URL = "amqp://admin:admin@rabbitmq:5672/"
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MessageRequest(BaseModel):
    message: str

@app.on_event("startup")
async def startup():
    try:
        app.state.connection = await aio_pika.connect_robust(RABBITMQ_URL)
        app.state.channel = await app.state.connection.channel()
        app.state.exchange = await app.state.channel.declare_exchange(
            "messages", aio_pika.ExchangeType.DIRECT
        )
        app.state.callback_queue = await app.state.channel.declare_queue(exclusive=True)
        app.state.futures = {}
        
        async def on_response(message: aio_pika.IncomingMessage):
            if message.correlation_id in app.state.futures:
                app.state.futures[message.correlation_id].set_result(message.body)
        
        await app.state.callback_queue.consume(on_response)
        logger.info("Gateway started with tracing and metrics")
    except Exception as e:
        logger.error(f"Startup failed: {e}")
        raise

@app.on_event("shutdown")
async def shutdown():
    if hasattr(app.state, 'connection'):
        await app.state.connection.close()

@app.post("/send/")
async def send_message(request: MessageRequest):
    try:
        correlation_id = str(uuid.uuid4())
        
        with tracer.start_as_current_span("gateway_send_message") as send_span:
            trace_id = format_trace_id(send_span.get_span_context().trace_id)
            logger.info(f"[TRACE_ID] {trace_id}")
            
            future = asyncio.get_event_loop().create_future()
            app.state.futures[correlation_id] = future

            payload = {
                "trace_id": trace_id,
                "message": request.message
            }

            send_span.set_attribute("custom.trace_id", trace_id)
            await app.state.exchange.publish(
                aio_pika.Message(
                    body=json.dumps(payload).encode(),
                    reply_to=app.state.callback_queue.name,
                    correlation_id=correlation_id
                ),
                routing_key="service_queue"
            )

        with tracer.start_as_current_span("gateway_wait_response"):
            response = await future
            
        decoded_response = json.loads(response)
        return decoded_response

    except Exception as e:
        logger.error(f"Request failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
# © Кравченко