import type { AgentExecutionStats } from "@/types/agent";

// Purely illustrative/static -- describes the real component pipeline
// `agents.retrieval`/`agents.confidence`/`agents.answer` implement
// (app/agents/retrieval/, app/agents/confidence.py, app/agents/answer/),
// NOT a live-monitored list of per-agent stats. Kept separate from
// `mockAgentStats` below (which mirrors the real, coarser-grained
// `agent_name` values `agent_executions` actually records) so the two are
// never confused as the same data source.
export const agentPipelineStages = [
  { key: "query_understanding", name: "Query Understanding", description: "Parses intent, entities, and constraints from the incoming question or incident." },
  { key: "hybrid_retrieval", name: "Hybrid Retrieval", description: "Runs lexical and vector search across knowledge and incident indices." },
  { key: "rrf_fusion", name: "RRF Fusion", description: "Fuses ranked result sets from multiple retrievers using reciprocal rank fusion." },
  { key: "cross_encoder", name: "Cross Encoder", description: "Reranks fused candidates with a cross-encoder relevance model." },
  { key: "context_assembly", name: "Context Assembly", description: "Assembles the highest-signal passages into a bounded context window." },
  { key: "confidence_evaluation", name: "Confidence Evaluation", description: "Scores retrieval sufficiency before generation is attempted." },
  { key: "answer_agent", name: "Answer Agent", description: "Generates the grounded answer, summary, or recommendation." },
  { key: "grounding_verification", name: "Grounding Verification", description: "Checks that every claim in the answer is supported by retrieved evidence." },
];

// Matches `app.agents.schemas.AgentExecutionStats` -- real `agent_name`
// values are the four graph entry points `app/agents/service.py` actually
// records (`answer_question`, `triage_incident`, `generate_postmortem`,
// `detect_knowledge_gaps`), not the eight fine-grained pipeline stages
// above -- those are internal steps within `answer_question`, not
// separately-tracked agents.
export const mockAgentStats: AgentExecutionStats[] = [
  { agentName: "answer_question", executionCount: 412, succeededCount: 398, failedCount: 14, avgConfidenceScore: 0.79, avgLatencySeconds: 2.4 },
  { agentName: "triage_incident", executionCount: 63, succeededCount: 60, failedCount: 3, avgConfidenceScore: 0.58, avgLatencySeconds: 4.1 },
  { agentName: "generate_postmortem", executionCount: 18, succeededCount: 18, failedCount: 0, avgConfidenceScore: null, avgLatencySeconds: 6.8 },
  { agentName: "detect_knowledge_gaps", executionCount: 30, succeededCount: 29, failedCount: 1, avgConfidenceScore: null, avgLatencySeconds: 1.9 },
];
