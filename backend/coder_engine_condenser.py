"""SDK-supported condenser extension; no provider monkeypatches or extra loops."""
from openhands.sdk import LLM
from openhands.sdk.context.condenser import LLMSummarizingCondenser
from coder_engine_tokens import count_request


class CountedLLM(LLM):
    def get_token_count(self, messages, tools=None, add_security_risk_prediction=False):
        if not messages: return 0
        return count_request({'messages': self.format_messages_for_llm(messages),
            'tools': [tool.to_openai_tool(add_security_risk_prediction=add_security_risk_prediction) for tool in tools or []],
            'think': (self.litellm_extra_body or {}).get('think', True)})


class EngineCondenser(LLMSummarizingCondenser):
    def condense(self, view, agent_llm=None):
        # Persisted SDK LLM fields deserialize as the public base LLM. Supply a
        # counting adapter at the supported condenser boundary on every run.
        counted = CountedLLM.model_validate(agent_llm.model_dump()) if agent_llm is not None else None
        return super().condense(view, agent_llm=counted)
