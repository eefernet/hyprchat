const LABELS={codebox:'Hephaestus',hephaestus:'Hephaestus',ollama:'Ollama',n8n:'N8N',searxng:'SearXNG',comfyui:'ComfyUI',stt:'Voice STT',tts:'Voice TTS',storage:'Storage'};

// Internal health keys stay stable so historical uptime remains continuous.
export const serviceLabel=name=>LABELS[name]||name;
