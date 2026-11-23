# Tracing and monitoring

All three processes (API, agent worker, gateway worker) export OpenTelemetry traces over OTLP/HTTP when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set. Without it, tracing is a no-op. The service name comes from
`OTEL_SERVICE_NAME`; workers default to `aieos-agents` and `aieos-gateway`.

## What one trace contains

The Temporal client carries the trace context in Temporal headers (`temporalio.contrib.opentelemetry`),
so one feature request shows up as one trace across processes:

```
POST /v1/orgs/{org_id}/projects/{project_id}/workflows        (aieos-api)
└─ StartWorkflow:FeatureDeliveryWorkflow
   └─ RunWorkflow / StartActivity:run_agent_step                (aieos-agents)
      └─ RunActivity:run_agent_step
         └─ agent.step                    agent, step, attempt, status
            ├─ chat <model>               provider, model, tokens in/out/cached, cost_usd, fallback, finish reason
            └─ execute_tool <tool>        tool, call id, turn, result size, error type
               └─ StartChildWorkflow:ToolCallWorkflow → RunActivity:execute_tool   (aieos-gateway)
                  └─ gateway <tool>       permission level, outcome (succeeded / denied / awaiting_approval / failed)
```

Attribute names follow the OpenTelemetry GenAI conventions where one exists (`gen_ai.provider.name`,
`gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.tool.name`, ...). Platform-specific ones use
the `aieos.` prefix (`aieos.cost_usd`, `aieos.workflow_id`, `aieos.agent_run_id`, `aieos.org_id`).

## What a trace never contains

Prompts, model output, tool arguments and tool results never go into spans. They can hold code,
secrets or personal data, and traces are kept and shown with weaker access control than the database.
Errors are recorded by exception type only (`error.type`), without the message or a stack trace.
Tests put canary strings in prompts, arguments, results and error messages, and fail if any of them
reaches a span.

The full record of what happened stays in the database (agent runs, checkpoints, tool executions,
audit events), under row-level security.

## Local

`make up` starts Jaeger. With `make up-api`, all three services export to it. Open
http://127.0.0.1:16686 and search by service, or by the `aieos.workflow_id` tag.

## AWS

The stack sets `OTEL_SERVICE_NAME` per service but no collector yet, so tracing is off there.
To turn it on, add an ADOT collector sidecar to the API, agents and gateway task definitions and set
`OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`. Sandbox tasks never get one: they have no route out.

Until then, the costs page of the dashboard (`/ui/#/costs`) and `GET /v1/orgs/{org}/costs` cover
spend, and CloudWatch Logs carries the structured logs (each with `trace_id` when tracing is on).
