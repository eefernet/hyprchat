"""Provider-neutral council calls with conservative per-run reservations."""
import json
import math
import model_providers as providers
import database as db
import config
import context_policy


def price(model):
    # Do not price an unknown future family using a coincidental prefix.
    import re
    for name, rates in sorted(providers.MODEL_PRICES_PER_MTOK.items(),key=lambda pair:-len(pair[0])):
        if model == name or re.fullmatch(re.escape(name)+r'-\d{4}-?\d{2}-?\d{2}',model):
            return rates
    return None


class Budget:
    def __init__(self, council, models):
        self.cap = float(council.get('cost_limit_usd') or 0)
        self.reserved = 0.0
        self.spent = 0.0
        self.unknown = False
        self.prompt_tokens = self.gen_tokens = 0
        cloud = [m for m in models if providers.is_cloud_model(m)]
        if cloud and (not council.get('cloud_enabled') or not math.isfinite(self.cap) or self.cap <= 0):
            raise ValueError('Cloud councils require explicit enablement and a positive per-run spending cap')
        if any(price(m) is None for m in cloud):
            raise ValueError('A selected cloud model has unknown pricing; the spending cap cannot be enforced')

    def reserve(self, model, messages, max_output):
        if not providers.is_cloud_model(model): return 0
        if self.unknown: raise ValueError('Previous cloud usage is uncertain; further paid calls are withheld')
        rates = price(model)
        if rates is None: raise ValueError('Unknown model pricing')
        # UTF-8 bytes overestimate text token counts. Include a generous envelope
        # allowance for provider formatting; bound generated tokens at the API.
        input_bound = len(json.dumps(messages,ensure_ascii=False).encode('utf8')) + 4096
        amount = (input_bound*rates[0] + max_output*rates[1])/1e6
        if self.spent+self.reserved+amount > self.cap:
            raise ValueError('Council spending cap reached; remaining paid calls are withheld')
        self.reserved += amount
        return amount

    def settle(self, model, amount, usage):
        self.reserved -= amount
        if usage is None:
            if amount:
                self.spent += amount
                self.unknown = True
            return
        incoming,outgoing=usage
        self.prompt_tokens += incoming; self.gen_tokens += outgoing
        rates=price(model) if providers.is_cloud_model(model) else None
        if rates: self.spent += (incoming*rates[0]+outgoing*rates[1])/1e6

    def summary(self):
        return {'prompt_tokens':self.prompt_tokens,'gen_tokens':self.gen_tokens,
                'estimated_cost_usd':round(self.spent,6),'cost_limit_usd':self.cap,
                'usage_uncertain':self.unknown}


async def stream(http, model, messages, budget, conv_id, *, max_output=2048):
    reservation=budget.reserve(model,messages,max_output)
    usage=None
    try:
        async for event in providers.stream_chat(http,model,messages,
                options={'num_ctx':context_policy.helper_context('extraction'),'num_predict':max_output},
                ollama_url=config.OLLAMA_URL):
            if event.get('type')=='usage':
                usage=(int(event.get('prompt_tokens') or 0),int(event.get('gen_tokens') or 0))
            yield event
    finally:
        budget.settle(model,reservation,usage)
        if usage and conv_id:
            await db.record_token_usage(conv_id,model,'Council',usage[0],usage[1])
