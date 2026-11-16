import React, { useEffect, useState } from 'react';
import { apiJson } from '../api.js';

// Under a model's context size setting: says so when the model itself has less than was asked for.
// Ollama loads a model with at most its own limit, and the chat now plans with that number too
// (backend/model_limits.py), so a larger setting does nothing for this model.
export default function ModelLimitNote({ model, asked, t }) {
  const [limit, setLimit] = useState(0);
  useEffect(() => {
    setLimit(0);
    if (!model) return undefined;
    const controller = new AbortController();
    apiJson(`/api/model-capabilities?model=${encodeURIComponent(model)}`, { signal: controller.signal })
      .then(caps => setLimit(Number(caps?.context_limit) || 0)).catch(() => {});
    return () => controller.abort();
  }, [model]);
  if (!limit || !(asked > limit)) return null;
  return <div role="note" style={{ fontSize: 'var(--hc-text-secondary)', color: t.warn || t.dim, marginTop: 6 }}>
    This model's own limit is {limit.toLocaleString()} tokens, so chats with it use {limit.toLocaleString()}, not {Number(asked).toLocaleString()}.
  </div>;
}
