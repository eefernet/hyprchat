// HyprChat frontend entry.
//
// This file is the former inline `<script type="text/babel">` body from
// dist/index.html, now compiled by Vite instead of in-browser Babel. The JSX
// below is UNCHANGED except for the heavy-library lazy-load triggers (mermaid /
// chart.js / html2pdf), which now call the ensureX() helpers from ./vendor.js.
//
// vendor.js must be imported first: it bundles the former CDN globals
// (Prism, KaTeX, renderMathInElement) and assigns them to window.* so the
// component code below — which references window.Prism / window.katex etc. —
// keeps working untouched.
import './vendor.js';

import React from 'react';
import * as ReactDOMFull from 'react-dom';
import { createRoot, hydrateRoot } from 'react-dom/client';
import {
  API,
  HC_USER_KEY,
  dismissHyprLoader,
  hcSessionKey,
  hcSetUserContext,
  hcStoredSession,
  hcStoredUserId,
  proxiedImageUrl,
  userScopedUrl,
} from './session.js';
import {
  BACKGROUND_EFFECTS,
  FONTS,
  THEMES,
  WELCOME_VERSION,
  fallbackWelcome,
  localDayKey,
} from './theme.js';
import {
  cleanResearchMarkdown,
  formatModelCtx,
  isCloudModelName,
  isEmbeddingModelName,
  isMoeModelName,
  modelContextLength,
  researchModelOptions,
} from './modelHelpers.js';
import { createSettingsSync } from './settingsSync.js';
import ModelPicker from './ModelPicker.jsx';
import AnalyticsPanel from './panels/AnalyticsPanel.jsx';
import PromptLibraryPanel from './panels/PromptLibraryPanel.jsx';
import BackgroundCanvas from './components/BackgroundCanvas.jsx';
import { IC, TIC } from './components/icons.jsx';
import {
  ChartBlock,
  CodeBlock,
  Collapsible,
  InlineColorSwatch,
  InlineMath,
  MermaidBlock,
  initMermaidTheme,
  looksLikeChartConfig,
  normalizeRenderableFences,
} from './components/markdownBlocks.jsx';
import {
  ArtifactStudioPanel,
  ImageStudioPanel,
} from './components/artifactComponents.jsx';
import {
  Chip,
  DaedalusSummary,
  MDWrap,
  MemoMD,
  MemoryProfilePanel,
  QuickSearchSourcesPanel,
  QuickSourceInlineChip,
  ResearchLiveStatus,
  RunCard,
  SandboxSection,
  Tip,
  ToolStatus,
  WorkflowCard,
  WorkspaceDetail,
  _activityGroupKey,
  _activityIsTerminal,
  _activityStatusKey,
  _daedalusRunIdsForMessage,
  _eventsHaveDaedalusFullBuild,
  _fmtElapsed,
  _isDaedalusFullBuildOutput,
  _isDaedalusOutput,
  _metaObj,
  _parseUtcishMs,
  _phaseFromEvent,
  _quickSearchPayloadFromEvents,
  _quickSourceForUrl,
  _qsHost,
  _runIdsFromEvents,
  citeOptsFor,
  CitationChip,
} from './components/hyprChatWidgets.jsx';

// The original code used the UMD ReactDOM global, which exposed BOTH
// createRoot (React 18 client) and createPortal (react-dom). Rebuild a single
// object with both so the bare `ReactDOM.createRoot` / `ReactDOM.createPortal`
// references in the body resolve exactly as before.
const ReactDOM = { ...ReactDOMFull, createRoot, hydrateRoot };

// ───────────────────────── original component body ─────────────────────────
const { useState, useEffect, useRef, useCallback, useMemo } = React;
const CHAT_HISTORY_RENDER_CHUNK = 80;

// ============================================================
function HyprChat(){
  const [tm,setTm]=useState(()=>{try{return localStorage.getItem("hc-theme")||"hyprflat";}catch{return "hyprflat";}});
  const [fi,setFi]=useState(()=>{try{return parseInt(localStorage.getItem("hc-font")||"0");}catch{return 0;}});
  const [tokenLimit,setTokenLimit]=useState(()=>{try{return parseInt(localStorage.getItem("hc-token-limit")||"0");}catch{return 0;}});
  const [numCtx,setNumCtx]=useState(()=>{try{return parseInt(localStorage.getItem("hc-num-ctx")||"0");}catch{return 0;}});
  const [defaultTemp,setDefaultTemp]=useState(()=>{try{return parseFloat(localStorage.getItem("hc-temp")||"0.7");}catch{return 0.7;}});
  const [defaultTopP,setDefaultTopP]=useState(()=>{try{return parseFloat(localStorage.getItem("hc-top-p")||"0.9");}catch{return 0.9;}});
  const [compactMode,setCompactMode]=useState(()=>{try{return localStorage.getItem("hc-compact")==="1";}catch{return false;}});
  const [autoExpandThink,setAutoExpandThink]=useState(()=>{try{return localStorage.getItem("hc-auto-expand-think")==="1";}catch{return false;}});
  const [bgEffect,setBgEffect]=useState(()=>{try{const saved=localStorage.getItem("hc-bg-effect");if(saved&&BACKGROUND_EFFECTS.some(e=>e.id===saved))return saved;return localStorage.getItem("hc-scanline")==="1"?"scanlines":"dots";}catch{return "dots";}});
  const [showNavLabels,setShowNavLabels]=useState(()=>{try{return localStorage.getItem("hc-nav-labels")!=="0";}catch{return true;}});
  const [showNavMore,setShowNavMore]=useState(false);
  const [thinkMode,setThinkMode]=useState(()=>{try{return localStorage.getItem("hc-think-mode")||"auto";}catch{return "auto";}});
  // Effort Level — global default (0=Blurt, 1=Ponder, 2=Forge, 3=Galaxy Brain)
  const [globalEffort,setGlobalEffort]=useState(()=>{try{return parseInt(localStorage.getItem("hc-effort-level")||"0")||0;}catch{return 0;}});
  // Per-chat override map: { [convId]: 0|1|2|3 }. Absent entry = inherit globalEffort.
  const [effortPerChat,setEffortPerChat]=useState(()=>{try{return JSON.parse(localStorage.getItem("hc-effort-per-chat")||"{}");}catch{return {};}});
  // Creative level names with emoji — index == rounds count
  const EFFORT_LEVELS=[
    {emoji:"💭",name:"Blurt",desc:"Raw answer, no review"},
    {emoji:"🧠",name:"Ponder",desc:"One self-review pass"},
    {emoji:"🔥",name:"Forge",desc:"Two refinement passes"},
    {emoji:"🌌",name:"Galaxy Brain",desc:"Max effort — three passes"},
  ];
  // Resolve effort for a given conv id (per-chat override, else global)
  const resolveEffort=(cid,overrides={})=>overrides.effort!==undefined?overrides.effort:(cid&&effortPerChat[cid]!==undefined?effortPerChat[cid]:(!cid&&pendingEffort!==null?pendingEffort:globalEffort));
  const [lastPersonaId,setLastPersonaId]=useState(()=>{try{return localStorage.getItem("hc-last-persona")||null;}catch{return null;}});
  const [fontSize,setFontSize]=useState(()=>{try{return parseInt(localStorage.getItem("hc-font-size")||"14");}catch{return 14;}});
  const [uiFontSize,setUiFontSize]=useState(()=>{try{return parseInt(localStorage.getItem("hc-ui-font-size")||"14");}catch{return 14;}});
  const [chatWidth,setChatWidth]=useState(()=>{try{return parseInt(localStorage.getItem("hc-chat-w")||"880");}catch{return 880;}});
  const [wsModel,setWsModel]=useState(()=>{try{return localStorage.getItem("hc-ws-model")||"qwen2.5:7b";}catch{return "qwen2.5:7b";}});
  const [dailyWelcome,setDailyWelcome]=useState(()=>{try{const c=JSON.parse(localStorage.getItem("hc-daily-welcome")||"{}");if(c.version===WELCOME_VERSION&&c.date===localDayKey()&&c.message)return c.message;}catch{}return fallbackWelcome();});
  const [planningModel,setPlanningModel]=useState(()=>{try{return localStorage.getItem("hc-planning-model")||"";}catch{return "";}});
  const [coderModel,setCoderModel]=useState(()=>{try{return localStorage.getItem("hc-coder-model")||"";}catch{return "";}});
  // Coder Bot v2 — per-agent model overrides. Empty = inherit from umbrella (Planning/Coder) or chat model.
  const [architectModel,setArchitectModel]=useState(()=>{try{return localStorage.getItem("hc-architect-model")||"";}catch{return "";}});
  const [reviewerModel, setReviewerModel] =useState(()=>{try{return localStorage.getItem("hc-reviewer-model") ||"";}catch{return "";}});
  const [acceptanceModel,setAcceptanceModel]=useState(()=>{try{return localStorage.getItem("hc-acceptance-model")||"";}catch{return "";}});
  const [builderModel,  setBuilderModel]  =useState(()=>{try{return localStorage.getItem("hc-builder-model")  ||"";}catch{return "";}});
  const [fixerModel,    setFixerModel]    =useState(()=>{try{return localStorage.getItem("hc-fixer-model")    ||"";}catch{return "";}});
  const [qaModel,       setQaModel]       =useState(()=>{try{return localStorage.getItem("hc-qa-model")       ||"";}catch{return "";}});
  const [coderBotModelsOpen,setCoderBotModelsOpen]=useState(()=>{try{return localStorage.getItem("hc-coderbot-models-open")==="1";}catch{return false;}});
  const [coderNumCtx,setCoderNumCtx]=useState(()=>{try{return parseInt(localStorage.getItem("hc-coder-num-ctx")||"8192");}catch{return 8192;}});
  const [researchNumCtx,setResearchNumCtx]=useState(()=>{try{return parseInt(localStorage.getItem("hc-research-num-ctx")||"40960");}catch{return 40960;}});
  const [openhandsEnabled,setOpenhandsEnabled]=useState(()=>{try{return localStorage.getItem("hc-openhands-enabled")!=="false";}catch{return true;}});
  const [openhandsMaxRounds,setOpenhandsMaxRounds]=useState(()=>{try{return parseInt(localStorage.getItem("hc-openhands-max-rounds")||"20");}catch{return 20;}});
  const [openhandsReasoningEffort,setOpenhandsReasoningEffort]=useState(()=>{try{return localStorage.getItem("hc-openhands-reasoning-effort")||"medium";}catch{return "medium";}});
  const [aiderEnabled,setAiderEnabled]=useState(()=>{try{return localStorage.getItem("hc-aider-enabled")!=="false";}catch{return true;}});
  const [aiderModel,setAiderModel]=useState(()=>{try{return localStorage.getItem("hc-aider-model")||"";}catch{return "";}});
  const [aiderAutoTest,setAiderAutoTest]=useState(()=>{try{return localStorage.getItem("hc-aider-auto-test")!=="false";}catch{return true;}});
  const [openhandsDisableThinking,setOpenhandsDisableThinking]=useState(()=>{try{return localStorage.getItem("hc-openhands-disable-thinking")!=="false";}catch{return true;}});
  const [quickSearchMode,setQuickSearchMode]=useState(()=>{try{return localStorage.getItem("hc-quick-search-mode")||"balanced";}catch{return "balanced";}});
  const [ctxTokens,setCtxTokens]=useState(0);
  const [genTokens,setGenTokens]=useState(0);
  const [previewFile,setPreviewFile]=useState(null);
  const [previewContent,setPreviewContent]=useState(null);
  const [previewError,setPreviewError]=useState(null);
  const [previewArchive,setPreviewArchive]=useState(null); // {filename, file_count, entries}
  const [pendingTheme,setPendingTheme]=useState(null);
  const [pendingFont,setPendingFont]=useState(null);
  const DEFAULT_LOADING_QUOTES=[
    {t:"The unexamined life is not worth living.",a:"Socrates"},
    {t:"I know that I know nothing.",a:"Socrates"},
    {t:"No great mind has ever existed without a touch of madness.",a:"Aristotle"},
    {t:"Knowing yourself is the beginning of all wisdom.",a:"Aristotle"},
    {t:"The soul becomes dyed with the colour of its thoughts.",a:"Marcus Aurelius"},
    {t:"Waste no more time arguing what a good man should be. Be one.",a:"Marcus Aurelius"},
    {t:"We suffer more often in imagination than in reality.",a:"Seneca"},
    {t:"No man ever steps in the same river twice.",a:"Heraclitus"},
    {t:"The journey of a thousand miles begins with one step.",a:"Lao Tzu"},
    {t:"When I let go of what I am, I become what I might be.",a:"Lao Tzu"},
    {t:"Be still, and know that I am God.",a:"Psalm 46:10"},
    {t:"The light shineth in darkness; and the darkness comprehended it not.",a:"John 1:5"},
    {t:"The kingdom of God is within you.",a:"Luke 17:21"},
    {t:"Let there be light: and there was light.",a:"Genesis 1:3"},
    {t:"And ye shall know the truth, and the truth shall make you free.",a:"John 8:32"},
    {t:"For now we see through a glass, darkly.",a:"1 Corinthians 13:12"},
    {t:"Sell your cleverness and buy bewilderment.",a:"Rumi"},
    {t:"The wound is the place where the Light enters you.",a:"Rumi"},
    {t:"Wherever you stand, be the soul of that place.",a:"Rumi"},
    {t:"The eye through which I see God is the same eye through which God sees me.",a:"Meister Eckhart"},
    {t:"If I have seen further it is by standing on the shoulders of Giants.",a:"Isaac Newton"},
    {t:"What we know is a drop, what we don't know is an ocean.",a:"Isaac Newton"},
    {t:"Mathematics is the language in which God has written the universe.",a:"Galileo Galilei"},
    {t:"Measure what is measurable, and make measurable what is not so.",a:"Galileo Galilei"},
    {t:"God does not play dice with the universe.",a:"Albert Einstein"},
    {t:"Imagination is more important than knowledge.",a:"Albert Einstein"},
    {t:"Science without religion is lame, religion without science is blind.",a:"Albert Einstein"},
    {t:"Somewhere, something incredible is waiting to be known.",a:"Carl Sagan"},
    {t:"We are a way for the cosmos to know itself.",a:"Carl Sagan"},
    {t:"Equipped with his five senses, man explores the universe around him.",a:"Edwin Hubble"},
  ];
  const [customQuotes,setCustomQuotes]=useState(()=>{try{const q=JSON.parse(localStorage.getItem("hc-custom-quotes")||"[]");const cleaned=Array.isArray(q)?q.filter(x=>x&&x.t&&x.a!=="HyprChat"):[];return cleaned.length?cleaned:DEFAULT_LOADING_QUOTES;}catch{return DEFAULT_LOADING_QUOTES;}});
  const [newQuoteText,setNewQuoteText]=useState("");
  const [newQuoteAttr,setNewQuoteAttr]=useState("");
  const [editQuoteIdx,setEditQuoteIdx]=useState(null);
  const [editQuoteText,setEditQuoteText]=useState("");
  const [editQuoteAttr,setEditQuoteAttr]=useState("");
  const [changelogContent,setChangelogContent]=useState(null);
  const [ollamaUrl,setOllamaUrl]=useState("");
  const [codeboxUrl,setCodeboxUrl]=useState("");
  const [searxngUrl,setSearxngUrl]=useState("");
  const [n8nUrl,setN8nUrl]=useState("");
  const [comfyuiUrl,setComfyuiUrl]=useState("");
  // Chat image generation defaults (Settings → Model & Generation).
  // Init from localStorage like sibling settings so the mount-time persist
  // effect doesn't clobber the stored value with "" before server hydration.
  const lsGet=(k,fb)=>{try{return localStorage.getItem(k)??fb;}catch{return fb;}};
  const [imgChatCkpt,setImgChatCkpt]=useState(()=>lsGet("hc-img-chat-ckpt",""));
  const [imgChatRes,setImgChatRes]=useState(()=>lsGet("hc-img-chat-res","1024x1024")||"1024x1024");
  const [imgChatVae,setImgChatVae]=useState(()=>lsGet("hc-img-chat-vae",""));
  const [imgChatWorkflow,setImgChatWorkflow]=useState(()=>lsGet("hc-img-chat-workflow",""));
  const [imgChatWorkflows,setImgChatWorkflows]=useState([]); // saved workflow names for the chat-default selector
  const [imgChatPrefix,setImgChatPrefix]=useState("");
  const [imgChatNeg,setImgChatNeg]=useState("");
  const [imgChatComposeModel,setImgChatComposeModel]=useState(()=>lsGet("hc-img-chat-compose",""));
  const [imgChatLists,setImgChatLists]=useState(null); // {checkpoints,vaes,settings} lazy-fetched for the dropdowns
  const [mdlPrefix,setMdlPrefix]=useState(""); // per-model prompt prefix being edited for imgChatCkpt
  const [mdlNeg,setMdlNeg]=useState("");
  const [sttUrl,setSttUrl]=useState("");
  const [ttsUrl,setTtsUrl]=useState("");
  const [ttsVoice,setTtsVoice]=useState("");
  const [ttsVoices,setTtsVoices]=useState([]);
  const [ttsAutoplay,setTtsAutoplay]=useState(()=>localStorage.getItem("hc-tts-autoplay")==="1");
  const [ollamaScanSshHost,setOllamaScanSshHost]=useState("");
  const [ollamaScanSshPort,setOllamaScanSshPort]=useState(22);
  const [ollamaScanSshUser,setOllamaScanSshUser]=useState("root");
  const [ollamaScanSshAuthMode,setOllamaScanSshAuthMode]=useState("key");
  const [ollamaScanSshKeyPath,setOllamaScanSshKeyPath]=useState("");
  const [ollamaScanSshPassword,setOllamaScanSshPassword]=useState("");
  const [ollamaScanSshHasPassword,setOllamaScanSshHasPassword]=useState(false);
  const [ollamaScanSshClearPassword,setOllamaScanSshClearPassword]=useState(false);
  const [modelProviders,setModelProviders]=useState({});
  const [providerKeys,setProviderKeys]=useState({openai:"",anthropic:""});
  const [providerBusy,setProviderBusy]=useState({});
  const [connectionsSaving,setConnectionsSaving]=useState(false);
  const [connectionsSaveState,setConnectionsSaveState]=useState(null); // saving|saved|failed
  const [ragSettings,setRagSettings]=useState({embed_model:"nomic-embed-text",chunk_size:500,chunk_overlap:50,top_k:6,max_context_chars:6000,research_top_k:4,research_max_chars:3000});
  const [kbPreview,setKbPreview]=useState(null);
  const [ragSaving,setRagSaving]=useState(false);
  const [ragSaveState,setRagSaveState]=useState(null); // saving|saved|failed
  const [ragStats,setRagStats]=useState(null);
  const [settingsPulse,setSettingsPulse]=useState(null); // {type,text}
  const settingsPulseTimer=useRef(null);
  const [modelParams,setModelParams]=useState(()=>{try{return JSON.parse(localStorage.getItem("hc-model-params")||"{}");}catch{return {};}});
  const [modelParamsOpen,setModelParamsOpen]=useState(null); // model name
  const [modelSearch,setModelSearch]=useState("");
  const [prompts,setPrompts]=useState(()=>{try{return JSON.parse(localStorage.getItem("hc-prompts")||"[]");}catch{return [];}});
  const [showPromptPicker,setShowPromptPicker]=useState(false);
  const [showQuickMenu,setShowQuickMenu]=useState(false);
  const [showConnectorPicker,setShowConnectorPicker]=useState(false);
  const [showEffortPicker,setShowEffortPicker]=useState(false);
  const [pendingEffort,setPendingEffort]=useState(null);
  const [pendingPersona,setPendingPersona]=useState(null);
  const [pendingModel,setPendingModel]=useState(()=>{try{return localStorage.getItem("hc-last-model")||"";}catch{return "";}});
  const modelChoiceRef=useRef({pending:"",byConv:{}});
  const [showExportMenu,setShowExportMenu]=useState(false);
  const effortPickerRef=useRef(null);
  const exportMenuRef=useRef(null);
  const [promptSearch,setPromptSearch]=useState("");
  const [connectorSearch,setConnectorSearch]=useState("");
  const [editPrompt,setEditPrompt]=useState(null); // {id, title, content, category}

  const [convTags,setConvTags]=useState(()=>{try{return JSON.parse(localStorage.getItem("hc-conv-tags")||"{}");}catch{return {};}});
  const [activeTagFilter,setActiveTagFilter]=useState(null);
  const [showTagEditor,setShowTagEditor]=useState(null); // conv id
  const [newTagInput,setNewTagInput]=useState("");
  const [previewWidth,setPreviewWidth]=useState(()=>{try{return parseInt(localStorage.getItem("hc-preview-w")||"420");}catch{return 420;}});
  const [pasteMode,setPasteMode]=useState(false);
  const [pasteCode,setPasteCode]=useState("");
  const [pasteToolName,setPasteToolName]=useState("");
  const [pasteToolDesc,setPasteToolDesc]=useState("");
  // Models page state
  const [modelsTab,setModelsTab]=useState("ollama"); // "ollama"|"hf"|"hyprfit"
  const [hfSearch,setHfSearch]=useState("");
  const [hfResults,setHfResults]=useState([]);
  const [hfLoading,setHfLoading]=useState(false);
  const [hfSelected,setHfSelected]=useState(null); // model object from search
  const [hfModelInfo,setHfModelInfo]=useState(null); // detailed info + files
  const [hfReadme,setHfReadme]=useState(null);
  const [hfReadmeLoading,setHfReadmeLoading]=useState(false);
  const [hfDownloading,setHfDownloading]=useState(false);
  const [hfDownloadName,setHfDownloadName]=useState("");
  const [downloads,setDownloads]=useState([]); // [{id,name,status,pct,message,error,completedBytes,totalBytes,startTime,speed,eta}]
  const [downloadsExpanded,setDownloadsExpanded]=useState(false);
  const [activityNow,setActivityNow]=useState(Date.now());
  const [hfSelectedFiles,setHfSelectedFiles]=useState([]); // filenames to download
  const [hfGgufOnly,setHfGgufOnly]=useState(true);
  const [hyprfit,setHyprfit]=useState(null);
  const [hyprfitLoading,setHyprfitLoading]=useState(false);
  const [hyprfitSaving,setHyprfitSaving]=useState(false);
  const [hyprfitRescanning,setHyprfitRescanning]=useState(false);
  const [hyprfitRescanStatus,setHyprfitRescanStatus]=useState(null);
  const [hyprfitProfile,setHyprfitProfile]=useState(null);
  const [hyprfitCategory,setHyprfitCategory]=useState("for_you");
  const t=THEMES[tm], font=FONTS[fi].v;
  const LIGHT_PAIRS={hyprflat:"oneLight",nord:"oneLight",catppuccin:"oneLight",gruvbox:"oneLight",tokyoNight:"oneLight",rosePine:"oneLight",dracula:"oneLight",midnight:"oneLight",terminal:"oneLight",cyberpunk:"oneLight",solarizedDark:"solarizedLight",materialOcean:"oneLight",ayuDark:"oneLight",oneLight:"hyprflat",solarizedLight:"solarizedDark"};
  const isLightTheme=tm==="oneLight"||tm==="solarizedLight";
  const settingsLoadedRef=useRef(false);
  const seenSettingEffectRef=useRef({});
  const skipSettingPatchRef=useRef({});
  const modelParamsSeenRef=useRef(false);
  const flashSettingsPulse=(text="Saved",type="success")=>{
    setSettingsPulse({text,type});
    if(settingsPulseTimer.current)clearTimeout(settingsPulseTimer.current);
    settingsPulseTimer.current=setTimeout(()=>setSettingsPulse(null),1800);
  };
  const {hydrateServerSetting,persistServerSetting}=createSettingsSync({
    api:API,
    settingsLoadedRef,
    seenSettingEffectRef,
    skipSettingPatchRef,
    flashSettingsPulse,
  });

  // Mermaid init — re-runs when theme/font changes; bumps epoch to force diagram re-render
  const [mermaidEpoch,setMermaidEpoch]=useState(0);
  useEffect(()=>{
    // Mermaid is lazy-loaded; if it isn't in yet, kick the load and re-run once it
    // resolves so every existing diagram re-themes/re-renders (epoch bump).
    if(!window.mermaid){window.ensureMermaid&&window.ensureMermaid().then(()=>setMermaidEpoch(e=>e+1));return;}
    try{
      initMermaidTheme(t,font);
      setMermaidEpoch(e=>e+1);
    }catch{}
  },[tm,fi]);

  useEffect(()=>{try{localStorage.setItem("hc-theme",tm);}catch{}},[tm]);
  useEffect(()=>{try{localStorage.setItem("hc-font",String(fi));}catch{}},[fi]);
  useEffect(()=>{try{localStorage.setItem("hc-token-limit",String(tokenLimit));}catch{}},[tokenLimit]);
  useEffect(()=>{persistServerSetting("hc-num-ctx","default_num_ctx",numCtx,String(numCtx));},[numCtx]);
  useEffect(()=>{try{localStorage.setItem("hc-temp",String(defaultTemp));}catch{}},[defaultTemp]);
  useEffect(()=>{try{localStorage.setItem("hc-top-p",String(defaultTopP));}catch{}},[defaultTopP]);
  useEffect(()=>{try{localStorage.setItem("hc-compact",compactMode?"1":"0");}catch{}},[compactMode]);
  useEffect(()=>{try{localStorage.setItem("hc-auto-expand-think",autoExpandThink?"1":"0");}catch{}},[autoExpandThink]);
  useEffect(()=>{try{localStorage.setItem("hc-bg-effect",bgEffect);localStorage.setItem("hc-scanline",bgEffect==="scanlines"?"1":"0");}catch{}},[bgEffect]);
  useEffect(()=>{try{localStorage.setItem("hc-nav-labels",showNavLabels?"1":"0");}catch{}},[showNavLabels]);
  useEffect(()=>{try{localStorage.setItem("hc-think-mode",thinkMode);}catch{}},[thinkMode]);
  useEffect(()=>{try{localStorage.setItem("hc-effort-level",String(globalEffort));}catch{}},[globalEffort]);
  useEffect(()=>{try{localStorage.setItem("hc-effort-per-chat",JSON.stringify(effortPerChat));}catch{}},[effortPerChat]);
  useEffect(()=>{try{localStorage.setItem("hc-font-size",fontSize);}catch{}},[fontSize]);
  useEffect(()=>{try{localStorage.setItem("hc-ui-font-size",uiFontSize);}catch{}},[uiFontSize]);
  useEffect(()=>{try{localStorage.setItem("hc-chat-w",chatWidth);}catch{}},[chatWidth]);
  useEffect(()=>{try{localStorage.setItem("hc-custom-quotes",JSON.stringify(customQuotes));}catch{}},[customQuotes]);
  useEffect(()=>{persistServerSetting("hc-ws-model","workspace_model",wsModel);},[wsModel]);
  useEffect(()=>{
    const date=localDayKey();
    const model=wsModel||"";
    try{
      const cached=JSON.parse(localStorage.getItem("hc-daily-welcome")||"{}");
      if(cached.version===WELCOME_VERSION&&cached.date===date&&cached.model===model&&cached.message){setDailyWelcome(cached.message);return;}
    }catch{}
    let cancelled=false;
    fetch(`${API}/api/daily-message`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model})})
      .then(r=>r.ok?r.json():null)
      .then(d=>{
        const msg=(d?.message||"").trim();
        if(cancelled||!msg)return;
        setDailyWelcome(msg);
        try{localStorage.setItem("hc-daily-welcome",JSON.stringify({version:WELCOME_VERSION,date,model,message:msg}));}catch{}
      })
      .catch(()=>{});
    return ()=>{cancelled=true;};
  },[wsModel]);
  useEffect(()=>{persistServerSetting("hc-planning-model","planning_model",planningModel);},[planningModel]);
  useEffect(()=>{persistServerSetting("hc-coder-model","coder_model",coderModel);},[coderModel]);
  // Coder Bot v2 per-agent overrides — sync each to localStorage + guarded PATCH /api/settings.
  useEffect(()=>{persistServerSetting("hc-architect-model","architect_model",architectModel);},[architectModel]);
  useEffect(()=>{persistServerSetting("hc-reviewer-model","reviewer_model",reviewerModel);},[reviewerModel]);
  useEffect(()=>{persistServerSetting("hc-acceptance-model","acceptance_model",acceptanceModel);},[acceptanceModel]);
  useEffect(()=>{persistServerSetting("hc-builder-model","builder_model",builderModel);},[builderModel]);
  useEffect(()=>{persistServerSetting("hc-fixer-model","fixer_model",fixerModel);},[fixerModel]);
  useEffect(()=>{persistServerSetting("hc-qa-model","qa_model",qaModel);},[qaModel]);
  useEffect(()=>{try{localStorage.setItem("hc-coderbot-models-open",coderBotModelsOpen?"1":"0");}catch{}},[coderBotModelsOpen]);
  useEffect(()=>{persistServerSetting("hc-coder-num-ctx","openhands_num_ctx",coderNumCtx,String(coderNumCtx));},[coderNumCtx]);
  useEffect(()=>{persistServerSetting("hc-research-num-ctx","research_num_ctx",researchNumCtx,String(researchNumCtx));},[researchNumCtx]);
  useEffect(()=>{persistServerSetting("hc-openhands-enabled","openhands_enabled",openhandsEnabled,String(openhandsEnabled));},[openhandsEnabled]);
  useEffect(()=>{persistServerSetting("hc-openhands-max-rounds","openhands_max_rounds",openhandsMaxRounds,String(openhandsMaxRounds));},[openhandsMaxRounds]);
  useEffect(()=>{persistServerSetting("hc-openhands-reasoning-effort","openhands_reasoning_effort",openhandsReasoningEffort);},[openhandsReasoningEffort]);
  useEffect(()=>{persistServerSetting("hc-openhands-disable-thinking","openhands_disable_thinking",openhandsDisableThinking,String(openhandsDisableThinking));},[openhandsDisableThinking]);
  useEffect(()=>{persistServerSetting("hc-aider-enabled","aider_enabled",aiderEnabled,String(aiderEnabled));},[aiderEnabled]);
  useEffect(()=>{persistServerSetting("hc-aider-model","aider_model",aiderModel);},[aiderModel]);
  useEffect(()=>{persistServerSetting("hc-aider-auto-test","aider_auto_test",aiderAutoTest,String(aiderAutoTest));},[aiderAutoTest]);
  useEffect(()=>{persistServerSetting("hc-quick-search-mode","quick_search_mode",quickSearchMode);},[quickSearchMode]);
  useEffect(()=>{persistServerSetting("hc-img-chat-ckpt","image_chat_checkpoint",imgChatCkpt);},[imgChatCkpt]);
  useEffect(()=>{persistServerSetting("hc-img-chat-res","image_chat_resolution",imgChatRes);},[imgChatRes]);
  useEffect(()=>{persistServerSetting("hc-img-chat-vae","image_chat_vae",imgChatVae);},[imgChatVae]);
  useEffect(()=>{persistServerSetting("hc-img-chat-workflow","image_chat_workflow",imgChatWorkflow);},[imgChatWorkflow]);
  useEffect(()=>{persistServerSetting("hc-img-chat-compose","image_chat_compose_model",imgChatComposeModel);},[imgChatComposeModel]);
  // Per-model prompt fields track the selected default image model
  useEffect(()=>{
    const s=(imgChatCkpt&&imgChatLists?.settings?.[imgChatCkpt])||{};
    setMdlPrefix(s.prompt_prefix||"");
    setMdlNeg(s.negative_prefix||"");
  },[imgChatCkpt,imgChatLists]);
  const saveModelPrefix=()=>{
    if(!imgChatCkpt)return;
    fetch(`${API}/api/images/model-settings/${encodeURIComponent(imgChatCkpt)}`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({prompt_prefix:mdlPrefix,negative_prefix:mdlNeg})})
      .then(r=>r.ok?r.json():Promise.reject(new Error("save failed")))
      .then(d=>{setImgChatLists(p=>p?{...p,settings:{...p.settings,[imgChatCkpt]:d.settings}}:p);flashSettingsPulse("Saved");})
      .catch(()=>flashSettingsPulse("Failed","error"));
  };
  useEffect(()=>{try{localStorage.setItem("hc-model-params",JSON.stringify(modelParams));}catch{}if(modelParamsSeenRef.current)flashSettingsPulse("Saved locally","success");else modelParamsSeenRef.current=true;},[modelParams]);
  useEffect(()=>{try{localStorage.setItem("hc-prompts",JSON.stringify(prompts));}catch{}},[prompts]);
  useEffect(()=>{try{localStorage.setItem("hc-conv-tags",JSON.stringify(convTags));}catch{}},[convTags]);
  const [convs,setConvs]=useState([]);
  const [actId,setActId]=useState(null);
  const [inp,setInp]=useState("");
  const [streaming,setStreaming]=useState(false);
  const [sidebar,setSidebar]=useState(true);
  const [models,setModels]=useState([]);
  const [modelDetails,setModelDetails]=useState({});
  const [modelInfoCache,setModelInfoCache]=useState({});
  const [modelInfoLoading,setModelInfoLoading]=useState(false);
  const [showModelfile,setShowModelfile]=useState(false);
  const [fixTemplateFamily,setFixTemplateFamily]=useState("chatml");
  const [fixingTemplate,setFixingTemplate]=useState(false);
  const [fixTemplateMsg,setFixTemplateMsg]=useState(null);
  const [makingToolModel,setMakingToolModel]=useState(null); // model name currently being processed
  const [sq,setSq]=useState("");
  const [ftsQuery,setFtsQuery]=useState("");
  const [ftsResults,setFtsResults]=useState([]);
  const [ftsLoading,setFtsLoading]=useState(false);
  const [showMessageSearch,setShowMessageSearch]=useState(false);
  const [analyticsData,setAnalyticsData]=useState(null);
  const [analyticsDays,setAnalyticsDays]=useState(30);
  const [analyticsGroup,setAnalyticsGroup]=useState("day");
  const [copied,setCopied]=useState(null);
  const [showScrollTop,setShowScrollTop]=useState(false);
  const [showScrollBottom,setShowScrollBottom]=useState(false);
  const [dragOver,setDragOver]=useState(false);
  const [prevDarkTheme,setPrevDarkTheme]=useState(()=>{try{return localStorage.getItem("hc-prev-dark")||"hyprflat";}catch{return "hyprflat";}});
  const [autoTitle,setAutoTitle]=useState(()=>{try{return localStorage.getItem("hc-auto-title")!=="false";}catch{return true;}});
  const [ghostMode,setGhostMode]=useState(false);
  const [pendingUseMemories,setPendingUseMemories]=useState(false);
  useEffect(()=>{try{localStorage.setItem("hc-auto-title",autoTitle?"true":"false");}catch{}},[autoTitle]);
  const [showSysPromptPicker,setShowSysPromptPicker]=useState(false);
  const [tokC,setTokC]=useState(0);
  const [tokS,setTokS]=useState(0);
  const [sessionTokens,setSessionTokens]=useState(0);
  const [panel,setPanel]=useState("chat");
  const [artifactFocusId,setArtifactFocusId]=useState(null);
  const previousPanelRef=useRef("chat");
  const [settingsTab,setSettingsTab]=useState("connections");
  // Lazy-load the checkpoint/VAE dropdown data and refresh saved workflow names
  // when the Model & Generation tab is opened with ComfyUI configured. (Must stay below the
  // panel/settingsTab declarations — hooks read them at render time.)
  useEffect(()=>{
    if(panel!=="settings"||settingsTab!=="generation"||!comfyuiUrl)return;
    if(!imgChatLists)fetch(`${API}/api/images/checkpoints`).then(r=>r.ok?r.json():null).then(d=>{if(d)setImgChatLists(d);}).catch(()=>{});
    fetch(`${API}/api/images/workflows`).then(r=>r.ok?r.json():null).then(d=>{if(d)setImgChatWorkflows(d.workflows||[]);}).catch(()=>{});
  },[panel,settingsTab,comfyuiUrl]);
  const [users,setUsers]=useState([]);
  const [currentUser,setCurrentUser]=useState(null);
  const [currentUserId,setCurrentUserId]=useState(()=>hcStoredUserId());
  const [authReady,setAuthReady]=useState(false);
  const [userGateOpen,setUserGateOpen]=useState(false);
  const [loginUserId,setLoginUserId]=useState(()=>hcStoredUserId());
  const [loginPassword,setLoginPassword]=useState("");
  const [loginError,setLoginError]=useState("");
  const [newUserName,setNewUserName]=useState("");
  const [newUserPassword,setNewUserPassword]=useState("");
  const [userNameDrafts,setUserNameDrafts]=useState({});
  const [userPasswordDrafts,setUserPasswordDrafts]=useState({});
  const [pendingToolIds,setPendingToolIds]=useState([]);
  const [evts,setEvts]=useState([]);
  const evtsRef=useRef([]);
  // Dedicated save buffer — accumulates events for the current stream regardless of chat switches
  const streamSaveEvtsRef=useRef([]);
  // Conversation id currently being streamed (null when idle). Set at sendMessages start, cleared on completion.
  const streamingCidRef=useRef(null);
  const [kbs,setKbs]=useState([]);
  const kbSaveTimersRef=useRef(new Map());
  const [uploadProgress,setUploadProgress]=useState({});  // {kbId: {filename, progress, status, error}}
  const [tools,setTools]=useState([]);
  const [mcs,setMcs]=useState([]);
  const [health,setHealth]=useState({});
  const [showHealthMonitor,setShowHealthMonitor]=useState(false);
  const [healthHistory,setHealthHistory]=useState(null);
  const [pullName,setPullName]=useState("");
  const [pullProg,setPullProg]=useState(null);
  const pullInputRef=useRef(null);
  const [editMc,setEditMc]=useState(null);
  const [profileTab,setProfileTab]=useState("agents");
  const [editTool,setEditTool]=useState(null);
  const [expandedBuiltin,setExpandedBuiltin]=useState(null);
  const [editKb,setEditKb]=useState(null);
  const [builtinTools,setBuiltinTools]=useState([]);
  const [connectorTools,setConnectorTools]=useState([]);
  const [mcpServers,setMcpServers]=useState([]);
  const [openapiConnectors,setOpenapiConnectors]=useState([]);
  const [mcpDraft,setMcpDraft]=useState(()=>({name:"",transport:"stdio",command:"",url:"",args:"[]",env:"{}",headers:"{}",allow_private:false}));
  const [openapiDraft,setOpenapiDraft]=useState(()=>({name:"",spec_url:"",base_url:"",auth:"{}",headers:"{}",allow_private:false}));
  const [connectorBusy,setConnectorBusy]=useState("");
  const [expandedPill,setExpandedPill]=useState(null);
  const [loadingConv,setLoadingConv]=useState(false);
  const [visibleMessageCounts,setVisibleMessageCounts]=useState({});
  const [editingMsg,setEditingMsg]=useState(null); // {index, content}
  const [regenPopover,setRegenPopover]=useState(null); // {index, model, temperature, personaId}
  const [toasts,setToasts]=useState([]); // [{id,type,text,detail,action}]
  const [confirmDialog,setConfirmDialog]=useState(null);
  const [confirmPhrase,setConfirmPhrase]=useState("");
  const [collapsedOutputs,setCollapsedOutputs]=useState(()=>new Set()); // Set of "msgIdx-outputIdx" keys
  const [attachments,setAttachments]=useState([]); // [{name, content}]
  const [coderProjUploading,setCoderProjUploading]=useState(false);
  const [coderProjInfo,setCoderProjInfo]=useState(null); // {name, file_count, language}
  const [coderWorkflows,setCoderWorkflows]=useState([]);
  const notify=useCallback(({type="info",text,detail,action,duration}={})=>{
    if(!text)return null;
    const id=`toast-${Date.now()}-${Math.random().toString(36).slice(2,7)}`;
    const ttl=duration===undefined?(type==="error"?8000:type==="warning"?6500:4200):duration;
    setToasts(ts=>[...ts.slice(-4),{id,type,text,detail,action}]);
    if(ttl>0)setTimeout(()=>setToasts(ts=>ts.filter(x=>x.id!==id)),ttl);
    return id;
  },[]);
  const confirmAction=useCallback((opts={})=>new Promise(resolve=>{
    setConfirmPhrase("");
    setConfirmDialog({
      title:opts.title||"Confirm action",
      body:opts.body||"",
      confirmLabel:opts.confirmLabel||"Confirm",
      cancelLabel:opts.cancelLabel||"Cancel",
      tone:opts.tone||"warning",
      requiredText:opts.requiredText||"",
      inputLabel:opts.inputLabel||"Type to confirm",
      resolve
    });
  }),[]);
  const addActivity=useCallback((item={})=>{
    const id=item.id||`act-${Date.now()}-${Math.random().toString(36).slice(2,7)}`;
    setDownloads(p=>[...p,{id,name:item.name||"Activity",kind:item.kind||"task",status:item.status||"creating",pct:item.pct||0,message:item.message||"Working...",error:item.error||null,completedBytes:item.completedBytes||0,totalBytes:item.totalBytes||0,startTime:Date.now(),speed:0,eta:0,chatId:item.chatId||null,href:item.href||null,actionLabel:item.actionLabel||null,retry:item.retry||null}]);
    setDownloadsExpanded(true);
    return id;
  },[]);
  const updateActivity=useCallback((id,upd)=>setDownloads(p=>p.map(d=>d.id===id?{...d,...upd}:d)),[]);
  const refreshCoderWorkflows=useCallback(async(cid=actId)=>{
    if(!cid)return;
    try{
      const r=await fetch(`${API}/api/coder/workflows?conversation_id=${cid}`);
      if(r.ok)setCoderWorkflows(await r.json());
    }catch{}
  },[actId]);
  const uploadCoderProject=async(f)=>{
    if(!actId||!f)return;
    setCoderProjUploading(true);setCoderProjInfo(null);
    const activityId=addActivity({name:`Project upload: ${f.name}`,kind:"upload",message:"Uploading archive...",pct:0,chatId:actId});
    // Placeholder chip so user sees something is happening
    const placeholderName=`📦 uploading ${f.name}...`;
    setAttachments(p=>[...p.filter(a=>!a.name.startsWith("📦 ")),{name:placeholderName,content:"[Project archive upload in progress]"}]);
    try{
      const fd=new FormData();fd.append("file",f);fd.append("conv_id",actId);
      updateActivity(activityId,{status:"uploading",message:"Sending archive...",pct:35});
      const r=await fetch(`${API}/api/coder/upload-project`,{method:"POST",body:fd});
      if(!r.ok){const err=await r.json().catch(()=>({detail:r.statusText}));throw new Error(err.detail||`HTTP ${r.status}`);}
      updateActivity(activityId,{status:"indexing",message:"Indexing project files...",pct:75});
      const data=await r.json();
      setCoderProjInfo({name:data.name,file_count:data.file_count,language:data.language,workflow_id:data.workflow_id,build_system:data.detected_build_system,test_cmd:data.detected_test_cmd,indexer_status:data.indexer_status});
      setAttachments(p=>[
        ...p.filter(a=>a.name!==placeholderName&&!a.name.startsWith("📦 project:")),
        {name:`📦 project: ${data.name} (${data.file_count} ${data.language} files)`,content:`[Uploaded project attached to sandbox at ${data.sandbox_path}; workflow_id=${data.workflow_id||"n/a"}]`}
      ]);
      updateActivity(activityId,{status:"done",message:"Project ready",pct:100});
      notify({type:"success",text:"Project uploaded",detail:`${data.name} is ready for Daedalus.`});
      refreshCoderWorkflows(actId);
    }catch(e){
      setAttachments(p=>p.filter(a=>a.name!==placeholderName));
      updateActivity(activityId,{status:"error",error:e.message,message:"Upload failed"});
      notify({type:"error",text:"Project upload failed",detail:e.message});
    }finally{
      setCoderProjUploading(false);
    }
  };
  const [workspaces,setWorkspaces]=useState([]);
  const [wsPanel,setWsPanel]=useState(false);
  const [activeWs,setActiveWs]=useState(null);
  const [wsDetail,setWsDetail]=useState(null);
  const [wsLoading,setWsLoading]=useState(false);

  // Deep Research workspace state
  const [researchTemplates,setResearchTemplates]=useState([]);
  const [researchReports,setResearchReports]=useState([]);
  const [activeResearchId,setActiveResearchId]=useState(null);
  const [activeResearch,setActiveResearch]=useState(null);
  const [researchLoading,setResearchLoading]=useState(false);
  const [researchRunning,setResearchRunning]=useState(false);
  const [researchEvents,setResearchEvents]=useState([]);
  const [researchDraft,setResearchDraft]=useState({query:"",focus:"",report_type:"analyst",depth:4,model:"",planner_model:"",auditor_model:"",kb_ids:[],inputs:[]});
  const [researchInputText,setResearchInputText]=useState("");
  const [researchReportFilter,setResearchReportFilter]=useState("");
  const [researchView,setResearchView]=useState("new");
  const [researchLiveMarkdown,setResearchLiveMarkdown]=useState("");
  const researchEventRef=useRef(null);
  const researchFileRef=useRef(null);

  // Council state
  const [councils,setCouncils]=useState([]);
  const [councilPresets,setCouncilPresets]=useState([]);
  const [activeCouncilId,setActiveCouncilId]=useState(null);
  const [councilReport,setCouncilReport]=useState(null); // {council_id, ...analysis data}
  const [councilReportLoading,setCouncilReportLoading]=useState(false);
  const [councilRunning,setCouncilRunning]=useState(false);
  const [councilResponses,setCouncilResponses]=useState({}); // {memberId: {model,content,done}}
  const [councilHostContent,setCouncilHostContent]=useState("");
  const [councilVotes,setCouncilVotes]=useState([]); // live votes during current stream
  const [councilVoting,setCouncilVoting]=useState(false); // voting phase active
  const [councilRound,setCouncilRound]=useState(null); // {round, total_rounds, label}
  const [councilKbStatus,setCouncilKbStatus]=useState(""); // KB retrieval status line
  // Ref to track active council stream — survives conversation switches
  const councilStreamRef=useRef(null); // {cid, running, responses, hostContent, votes, voting, round}
  const [expandedRounds,setExpandedRounds]=useState({}); // {"turnIdx-roundNum": true/false}
  const [councilSuggestions,setCouncilSuggestions]=useState([]); // suggested prompts for council
  const [councilSugLoading,setCouncilSugLoading]=useState(false);
  const [editMember,setEditMember]=useState(null); // member id being edited
  const [editMemberForm,setEditMemberForm]=useState({model:"",model_config_id:"",system_prompt:"",persona_name:""});

  const [quickResults,setQuickResults]=useState([]); // per send
  const [searchLoading,setSearchLoading]=useState(false);
  const [quickSearchError,setQuickSearchError]=useState(null);
  const [currentRunNotice,setCurrentRunNotice]=useState(null); // {type,label,detail,at}
  const [composerFocused,setComposerFocused]=useState(false);

  const chatEnd=useRef(null),inpRef=useRef(null),abortR=useRef(null),sseR=useRef(null),fileRef=useRef(null),loadReqRef=useRef(0),chatScrollRef=useRef(null),dlPanelRef=useRef(null),quickMenuRef=useRef(null),promptPickerRef=useRef(null),connectorPickerRef=useRef(null),actIdRef=useRef(actId),titleSearchRef=useRef(null),ftsRef=useRef(null),importRef=useRef(null),charCardImportRef=useRef(null);
  actIdRef.current=actId;
  // ── Voice: mic recording (STT) + speech playback (TTS) ──
  const [recording,setRecording]=useState(false);
  const [transcribing,setTranscribing]=useState(false);
  const [speakingMid,setSpeakingMid]=useState(null);
  const [ttsLoadingMid,setTtsLoadingMid]=useState(null);
  const [ttsPhase,setTtsPhase]=useState("");
  const mediaRecRef=useRef(null),recChunksRef=useRef([]),audioRef=useRef(null),ttsAbortRef=useRef(null),ttsPreparedRef=useRef(null);
  const releaseTtsAudio=(audio)=>{
    if(!audio)return;
    try{audio.pause();audio.removeAttribute("src");audio.load();}catch{}
    if(audio._url)URL.revokeObjectURL(audio._url);
  };
  const stopSpeaking=()=>{
    if(ttsAbortRef.current){ttsAbortRef.current.abort();ttsAbortRef.current=null;}
    const audios=new Set([audioRef.current,ttsPreparedRef.current?.audio].filter(Boolean));
    audioRef.current=null;ttsPreparedRef.current=null;
    audios.forEach(releaseTtsAudio);
    setSpeakingMid(null);setTtsLoadingMid(null);setTtsPhase("");
  };
  const ttsAbortError=()=>{const e=new Error("Aborted");e.name="AbortError";return e;};
  const mediaErrorDetail=(audio)=>{
    const code=audio?.error?.code;
    if(code===1)return "Audio loading was aborted.";
    if(code===2)return "The browser hit a network error while loading the generated audio.";
    if(code===3)return "The browser could not decode the generated audio.";
    if(code===4)return "The browser cannot play the generated audio format.";
    return "The browser could not load the generated audio stream.";
  };
  const ttsMediaError=(audio,fallback="Audio stream failed")=>{
    const e=new Error(audio?.error?mediaErrorDetail(audio):fallback);
    e.name=audio?.error?.code===4?"NotSupportedError":"MediaError";
    e.mediaError=audio?.error||null;
    return e;
  };
  const speechErrorDetail=(e)=>{
    const msg=String(e?.message||e||"");
    if(e?.name==="NotAllowedError")return "Browser blocked playback. Click speak again to play the prepared audio.";
    if(e?.name==="NotSupportedError")return msg||"The browser cannot play the generated audio format.";
    if(e?.name==="MediaError")return msg||"The browser could not load the generated audio stream.";
    if(/first audio|no audio/i.test(msg))return "TTS is responding slowly; Kokoro has not returned audio yet.";
    if(/504/.test(msg)||/timeout|timed out/i.test(msg))return "The TTS service timed out before audio could start.";
    if(/503/.test(msg))return "Text-to-speech is not configured or unavailable.";
    if(/502/.test(msg))return "The TTS service rejected the request or could not be reached.";
    return msg||"The browser could not load the generated audio stream.";
  };
  const cleanTtsText=(text)=>String(text||"")
    .replace(/<think>[\s\S]*?<\/think>/gi," ")
    .replace(/(?:^|\n)[ \t]{0,3}```[\s\S]*?(?:\n[ \t]{0,3}```|$)/g," ")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g,"$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g,"$1")
    .replace(/https?:\/\/\S+/g," ")
    .replace(/\[\^?\d{1,2}\]/g," ")
    .replace(/[`*_#>|]/g," ")
    .replace(/\s+/g," ")
    .trim();
  const waitForAudioStart=(audio,signal,timeoutMs=20000,opts={})=>new Promise((resolve,reject)=>{
    if(signal?.aborted){reject(ttsAbortError());return;}
    let done=false;
    const finish=(fn,val)=>{if(done)return;done=true;clearTimeout(timer);audio.removeEventListener("loadeddata",onReady);audio.removeEventListener("canplay",onReady);audio.removeEventListener("playing",onPlaying);audio.removeEventListener("error",onError);audio.removeEventListener("abort",onMediaAbort);signal?.removeEventListener("abort",onAbort);fn(val);};
    const onReady=()=>{opts.onReady&&opts.onReady();};
    const onPlaying=()=>{opts.onPlaying&&opts.onPlaying();finish(resolve);};
    const onError=()=>finish(reject,ttsMediaError(audio));
    const onMediaAbort=()=>finish(reject,ttsMediaError(audio,"Audio loading was aborted"));
    const onAbort=()=>finish(reject,ttsAbortError());
    const timer=setTimeout(()=>finish(reject,new Error("TTS first audio timeout")),timeoutMs);
    audio.addEventListener("loadeddata",onReady);
    audio.addEventListener("canplay",onReady);
    audio.addEventListener("playing",onPlaying,{once:true});
    audio.addEventListener("error",onError,{once:true});
    audio.addEventListener("abort",onMediaAbort,{once:true});
    signal?.addEventListener("abort",onAbort,{once:true});
    if(audio.readyState>=2)onReady();
    let p;
    try{p=audio.play();}catch(e){finish(reject,e);return;}
    if(p&&p.then)p.then(()=>{if(!done&&!audio.paused)onPlaying();}).catch(e=>finish(reject,e));
  });
  const waitForAudioEnd=(audio,signal,opts={})=>new Promise((resolve,reject)=>{
    if(signal?.aborted){reject(ttsAbortError());return;}
    if(audio.ended){opts.onEnded&&opts.onEnded();resolve();return;}
    let done=false;
    const finish=(fn,val)=>{if(done)return;done=true;audio.removeEventListener("ended",onEnded);audio.removeEventListener("error",onError);audio.removeEventListener("abort",onMediaAbort);signal?.removeEventListener("abort",onAbort);fn(val);};
    const onEnded=()=>{opts.onEnded&&opts.onEnded();finish(resolve);};
    const onError=()=>finish(reject,ttsMediaError(audio));
    const onMediaAbort=()=>finish(reject,ttsMediaError(audio,"Audio playback was aborted"));
    const onAbort=()=>finish(reject,ttsAbortError());
    audio.addEventListener("ended",onEnded,{once:true});
    audio.addEventListener("error",onError,{once:true});
    audio.addEventListener("abort",onMediaAbort,{once:true});
    signal?.addEventListener("abort",onAbort,{once:true});
  });
  const playTtsSequence=async({mid,chunks,startIndex=0,firstAudio=null,firstHasSource=false,textKey})=>{
    const ctrl=new AbortController();ttsAbortRef.current=ctrl;
    let currentAudio=null,currentPrepared=null;
    try{
      for(let ci=startIndex;ci<chunks.length;ci++){
        if(ttsAbortRef.current!==ctrl||ctrl.signal.aborted)throw ttsAbortError();
        const audio=(ci===startIndex&&firstAudio)?firstAudio:new Audio();
        currentAudio=audio;
        audioRef.current=audio;
        audio.preload="auto";
        currentPrepared={mid,chunks,index:ci,audio,voice:ttsVoice||"",textKey};
        setTtsLoadingMid(mid);setTtsPhase(chunks.length>1?`generating ${ci+1}/${chunks.length}`:"generating");
        if(!(ci===startIndex&&firstHasSource&&audio.src)){
          const r=await fetch(`${API}/api/audio/speech/request`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text:chunks[ci],voice:ttsVoice}),signal:ctrl.signal});
          if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.detail||`HTTP ${r.status}`);}
          const d=await r.json();
          if(ttsAbortRef.current!==ctrl||ctrl.signal.aborted)throw ttsAbortError();
          const streamUrl=d.url?.startsWith("http")?d.url:`${API}${d.url||`/api/audio/speech/${d.id}`}`;
          audio.src=streamUrl;
          try{audio.load();}catch{}
        }
        if(ttsAbortRef.current!==ctrl||ctrl.signal.aborted)throw ttsAbortError();
        setTtsLoadingMid(mid);setTtsPhase(chunks.length>1?`loading ${ci+1}/${chunks.length}`:"loading");
        await waitForAudioStart(audio,ctrl.signal,20000,{
          onReady:()=>{if(ttsAbortRef.current===ctrl){setTtsLoadingMid(mid);setTtsPhase(chunks.length>1?`ready ${ci+1}/${chunks.length}`:"ready");}},
          onPlaying:()=>{if(ttsAbortRef.current===ctrl){setTtsLoadingMid(null);setSpeakingMid(mid);setTtsPhase(chunks.length>1?`playing ${ci+1}/${chunks.length}`:"playing");}},
        });
        if(ttsAbortRef.current!==ctrl||ctrl.signal.aborted)throw ttsAbortError();
        currentPrepared=null;
        await waitForAudioEnd(audio,ctrl.signal,{onEnded:()=>{if(audioRef.current===audio)audioRef.current=null;}});
        if(audioRef.current===audio)audioRef.current=null;
        currentAudio=null;
      }
      if(ttsAbortRef.current===ctrl)ttsAbortRef.current=null;
      setSpeakingMid(null);setTtsLoadingMid(null);setTtsPhase("");
    }catch(e){
      if(ttsAbortRef.current===ctrl)ttsAbortRef.current=null;
      if(e.name==="NotAllowedError"&&currentPrepared?.audio){
        ttsPreparedRef.current=currentPrepared;
        audioRef.current=currentPrepared.audio;
        notify({type:"warning",text:"Browser blocked playback",detail:"Click speak again to play the prepared audio.",duration:6000});
      }else{
        if(currentAudio&&audioRef.current===currentAudio){releaseTtsAudio(currentAudio);audioRef.current=null;}
        if(e.name!=="AbortError")notify({type:"error",text:"Speech failed",detail:speechErrorDetail(e)});
      }
      setTtsLoadingMid(null);setSpeakingMid(null);setTtsPhase("");
    }
  };
  const speak=async(text,mid)=>{
    if(!ttsUrl||!text)return;
    const textKey=cleanTtsText(text);
    const prepared=ttsPreparedRef.current;
    if(prepared&&(prepared.mid!==mid||prepared.textKey!==textKey||prepared.voice!==(ttsVoice||""))){
      if(audioRef.current===prepared.audio)audioRef.current=null;
      releaseTtsAudio(prepared.audio);
      ttsPreparedRef.current=null;
    }
    if(ttsPreparedRef.current?.mid===mid&&ttsPreparedRef.current?.textKey===textKey&&ttsPreparedRef.current?.voice===(ttsVoice||"")){
      const ready=ttsPreparedRef.current;
      ttsPreparedRef.current=null;
      await playTtsSequence({mid,chunks:ready.chunks,startIndex:ready.index,firstAudio:ready.audio,firstHasSource:true,textKey});
      return;
    }
    if(speakingMid===mid||ttsLoadingMid===mid){stopSpeaking();return;}
    if(!textKey)return;
    const chunks=[textKey];
    stopSpeaking();
    const firstAudio=new Audio();
    firstAudio.preload="auto";
    audioRef.current=firstAudio;
    await playTtsSequence({mid,chunks,startIndex:0,firstAudio,textKey});
  };
  const toggleRecording=async()=>{
    if(recording){try{mediaRecRef.current?.stop();}catch{}return;}
    if(!navigator.mediaDevices?.getUserMedia){notify({type:"warning",text:"Microphone unavailable",detail:"getUserMedia needs a secure context — use HTTPS or allow this origin in chrome://flags/#unsafely-treat-insecure-origin-as-secure"});return;}
    let stream;
    try{stream=await navigator.mediaDevices.getUserMedia({audio:true});}
    catch(e){notify({type:"warning",text:"Microphone unavailable",detail:e.name==="NotAllowedError"?"Permission denied":e.name==="NotFoundError"?"No microphone found":String(e.message||e)});return;}
    const mime=window.MediaRecorder&&MediaRecorder.isTypeSupported&&MediaRecorder.isTypeSupported("audio/webm;codecs=opus")?"audio/webm;codecs=opus":"";
    let rec;
    try{rec=new MediaRecorder(stream,mime?{mimeType:mime}:undefined);}
    catch(e){stream.getTracks().forEach(tr=>tr.stop());notify({type:"error",text:"Recording not supported",detail:String(e.message||e)});return;}
    recChunksRef.current=[];
    rec.ondataavailable=e=>{if(e.data&&e.data.size)recChunksRef.current.push(e.data);};
    rec.onstop=async()=>{
      stream.getTracks().forEach(tr=>tr.stop());
      setRecording(false);
      const blob=new Blob(recChunksRef.current,{type:rec.mimeType||"audio/webm"});
      recChunksRef.current=[];
      if(blob.size<800)return; // accidental tap — nothing recorded
      setTranscribing(true);
      try{
        const bt=blob.type||"";
        const ext=bt.includes("mp4")?"m4a":bt.includes("ogg")?"ogg":"webm";
        const fd=new FormData();fd.append("file",blob,`recording.${ext}`);
        const r=await fetch(`${API}/api/audio/transcribe`,{method:"POST",body:fd});
        const d=await r.json().catch(()=>({}));
        if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
        if(d.text){setInp(p=>p?`${p} ${d.text}`:d.text);setTimeout(()=>{if(inpRef.current){inpRef.current.style.height="auto";inpRef.current.style.height=Math.min(inpRef.current.scrollHeight,120)+"px";inpRef.current.focus();}},0);}
      }catch(e){notify({type:"error",text:"Transcription failed",detail:String(e.message||e)});}
      setTranscribing(false);
    };
    mediaRecRef.current=rec;
    rec.start();
    setRecording(true);
  };
  const act = convs.find(c=>c.id===actId);
  const isGhostConv=(c)=>!!c?.ephemeral||String(c?.id||"").startsWith("ghost-");
  const ghostActive=ghostMode||isGhostConv(act);
  const DAEDALUS_AVATAR="data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHZpZXdCb3g9JzAgMCAxMDAgMTAwJz48ZGVmcz48bGluZWFyR3JhZGllbnQgaWQ9J2cnIHgxPScxMicgeTE9JzgnIHgyPSc4OCcgeTI9JzkyJyBncmFkaWVudFVuaXRzPSd1c2VyU3BhY2VPblVzZSc+PHN0b3Agc3RvcC1jb2xvcj0nIzBiMTIyMCcvPjxzdG9wIG9mZnNldD0nLjU1JyBzdG9wLWNvbG9yPScjMWQyYTQ0Jy8+PHN0b3Agb2Zmc2V0PScxJyBzdG9wLWNvbG9yPScjNGIyZDczJy8+PC9saW5lYXJHcmFkaWVudD48bGluZWFyR3JhZGllbnQgaWQ9J2EnIHgxPScyNCcgeTE9JzE4JyB4Mj0nNzgnIHkyPSc4NCcgZ3JhZGllbnRVbml0cz0ndXNlclNwYWNlT25Vc2UnPjxzdG9wIHN0b3AtY29sb3I9JyM4ZmQ4ZmYnLz48c3RvcCBvZmZzZXQ9Jy41NScgc3RvcC1jb2xvcj0nI2E4OGNmZicvPjxzdG9wIG9mZnNldD0nMScgc3RvcC1jb2xvcj0nIzZmZmZkMicvPjwvbGluZWFyR3JhZGllbnQ+PC9kZWZzPjxyZWN0IHdpZHRoPScxMDAnIGhlaWdodD0nMTAwJyByeD0nMTgnIGZpbGw9J3VybCgjZyknLz48cGF0aCBkPSdNMTggNDloMTVNNjcgNDloMTVNNTAgMTh2MTNNNTAgNjl2MTNNMjkgMjlsLTktOU03MSA3MWw5IDknIHN0cm9rZT0nIzZmZmZkMicgc3Ryb2tlLXdpZHRoPSczJyBzdHJva2UtbGluZWNhcD0ncm91bmQnIG9wYWNpdHk9Jy43MicvPjxwYXRoIGQ9J00yOCA3MlYyOGgyMmMxNiAwIDI2IDkgMjYgMjJTNjYgNzIgNTAgNzJIMjh6JyBmaWxsPSdub25lJyBzdHJva2U9J3VybCgjYSknIHN0cm9rZS13aWR0aD0nNycgc3Ryb2tlLWxpbmVqb2luPSdyb3VuZCcvPjxwYXRoIGQ9J000MiAzNnYyOGg4YzkgMCAxNS01IDE1LTE0UzU5IDM2IDUwIDM2aC04eicgZmlsbD0nIzExMTgyNycgc3Ryb2tlPScjZDhlNmZmJyBzdHJva2Utd2lkdGg9JzMnIHN0cm9rZS1saW5lam9pbj0ncm91bmQnLz48Y2lyY2xlIGN4PSc1MCcgY3k9JzUwJyByPSc1JyBmaWxsPScjNmZmZmQyJy8+PGNpcmNsZSBjeD0nMjAnIGN5PScyMCcgcj0nNCcgZmlsbD0nI2E4OGNmZicvPjxjaXJjbGUgY3g9JzgwJyBjeT0nODAnIHI9JzQnIGZpbGw9JyM4ZmQ4ZmYnLz48L3N2Zz4=";
  const isDaedalusProfileName=(name)=>String(name||"").toLowerCase().includes("daedalus");
  const isCoderPersonaName = (name)=>{const n=(name||"").toLowerCase();return n.includes("coder")||n.includes("daedalus");};
  const avatarSrc=(avatar)=>avatar?(String(avatar).startsWith("blob:")||String(avatar).startsWith("data:")||String(avatar).startsWith("http")?avatar:`${API}${avatar}`):"";
  const profileAvatar=(mc)=>mc?.parameters?.avatar||(isDaedalusProfileName(mc?.name)?DAEDALUS_AVATAR:null);
  const parseTags=(v)=>Array.isArray(v)?v.map(x=>String(x).trim()).filter(Boolean):String(v||"").split(",").map(x=>x.trim()).filter(Boolean);
  const tagsToText=(v)=>parseTags(v).join(", ");
  const PERSONA_RATINGS=[
    ["G","🧸 G","All-ages. No profanity, sexual content, graphic violence, drugs, or mature themes."],
    ["PG","🍿 PG","Mild jokes, tiny scares, and gentle conflict. No explicit adult content."],
    ["PG-13","😏 PG-13","Teen-level edge: mild profanity, flirting, suggestive jokes, and non-graphic mature themes."],
    ["R","🔥 R","Adult language and mature themes are allowed, but keep sexual content non-explicit."],
    ["NC-17","🚨 NC-17","Explicit adult-only themes may be used for consenting adults; avoid minors, coercion, and illegal content."],
    ["Unrated","💋 Unrated (max XXX)","Broadest adult setting: explicit consenting-adult NSFW is allowed within safety boundaries."],
  ];
  const normalizePersonaRating=(v)=>{
    const s=String(v||"PG-13").trim().toLowerCase().replace(/\s+/g,"");
    if(["g","general","allages"].includes(s))return"G";
    if(["pg"].includes(s))return"PG";
    if(["pg13","pg-13","teen"].includes(s))return"PG-13";
    if(["r","mature"].includes(s))return"R";
    if(["nc17","nc-17"].includes(s))return"NC-17";
    if(["unrated","xxx","maxxxx"].includes(s))return"Unrated";
    return"PG-13";
  };
  const personaRatingMeta=(v)=>PERSONA_RATINGS.find(([key])=>key===normalizePersonaRating(v))||PERSONA_RATINGS[2];
  const personaRatingLabel=(v)=>personaRatingMeta(v)[1];
  const personaRatingGuidance=(v)=>personaRatingMeta(v)[2];
  const normalizePersonaThinkingMode=(v)=>{
    const s=String(v||"auto").trim().toLowerCase();
    if(["on","true","enable","enabled","1"].includes(s))return"on";
    if(["off","false","disable","disabled","0"].includes(s))return"off";
    return"auto";
  };
  const personaThinkingLabel=(v)=>({auto:"Auto",on:"On",off:"Off"}[normalizePersonaThinkingMode(v)]||"Auto");
  const personaThinkingGuidance=(v)=>({
    auto:"Uses the global Thinking Mode setting and model default.",
    on:"Forces thinking tokens on for this Persona when the model supports them.",
    off:"Disables thinking tokens for this Persona to reduce latency and empty reasoning-only replies.",
  }[normalizePersonaThinkingMode(v)]||"Uses the global Thinking Mode setting and model default.");
  const getProfileType=(mc)=>{
    const params=mc?.parameters||{};
    if(params.profile_type==="agent"||params.profile_type==="persona")return params.profile_type;
    const n=(mc?.name||"").toLowerCase();
    if(n.includes("based bot")||n.includes("based")||n.includes("gamer bro")||n.includes("tyler"))return "persona";
    const p=params.persona||{};
    const personaKeys=["personality","scenario","first_message","example_dialogue","lore","rating"];
    if(personaKeys.some(k=>String(p[k]||params[k]||"").trim()))return "persona";
    if(parseTags(p.tags||params.tags).length&&!(mc?.tool_ids||[]).length)return "persona";
    if(["daedalus","coder","deep researcher","conspiracy","reviewer","researcher","automation"].some(x=>n.includes(x)))return "agent";
    if((mc?.tool_ids||[]).length||(mc?.kb_ids||[]).length)return "agent";
    return "agent";
  };
  const normalizeProfileParams=(mc)=>{
    const params={...(mc?.parameters||{})};
    const type=params.profile_type==="agent"||params.profile_type==="persona"?params.profile_type:getProfileType(mc);
    params.profile_type=type;
    if(type==="persona"){
      const p={...(params.persona||{})};
      params.persona={
        description:p.description??params.description??"",
        personality:p.personality??params.personality??"",
        appearance:p.appearance??params.appearance??"",
        scenario:p.scenario??params.scenario??"",
        first_message:p.first_message??params.first_message??"",
        example_dialogue:p.example_dialogue??params.example_dialogue??"",
        lore:p.lore??params.lore??"",
        tags:parseTags(p.tags??params.tags),
        rating:normalizePersonaRating(p.rating??params.rating),
        thinking_mode:normalizePersonaThinkingMode(p.thinking_mode??params.thinking_mode),
        advanced_prompt:p.advanced_prompt??params.advanced_prompt??"",
      };
      params.description=params.persona.description||params.description||"";
    }
    return params;
  };
  const isAgentProfile=(mc)=>getProfileType(mc)==="agent";
  const isPersonaProfile=(mc)=>getProfileType(mc)==="persona";
  const buildPersonaPrompt=(fields={})=>{
    const name=(fields.name||"this persona").trim();
    const parts=[`You are ${name}. Stay fully in this persona's identity, voice, and conversational style.`];
    if(fields.description)parts.push(`Tagline / summary:\n${fields.description}`);
    if(fields.personality)parts.push(`Personality:\n${fields.personality}`);
    if(fields.scenario)parts.push(`Scenario:\n${fields.scenario}`);
    if(fields.lore)parts.push(`Lore / world notes:\n${fields.lore}`);
    if(parseTags(fields.tags).length)parts.push(`Style tags:\n${parseTags(fields.tags).join(", ")}`);
    if(fields.rating)parts.push(`Content rating / boundaries:\n${personaRatingLabel(fields.rating)}\n${personaRatingGuidance(fields.rating)}`);
    if(fields.first_message)parts.push(`First message to use when starting a fresh conversation:\n${fields.first_message}`);
    if(fields.example_dialogue)parts.push(`Example dialogue:\n${fields.example_dialogue}`);
    if(fields.advanced_prompt)parts.push(`Advanced system prompt:\n${fields.advanced_prompt}`);
    return parts.filter(Boolean).join("\n\n");
  };
  const profileDescription=(mc)=>{
    const params=normalizeProfileParams(mc);
    const p=params.persona||{};
    const explicit=(p.description||params.description||"").trim();
    if(explicit)return explicit;
    const n=(mc.name||"").toLowerCase();
    if(getProfileType(mc)==="persona"){
      if(n.includes("based")||n.includes("gamer bro")||n.includes("tyler"))return "Gamer-bro persona with blunt, meme-fluent, based commentary for casual chat.";
      return "Roleplay persona focused on identity, voice, tone, scenario, and conversational style.";
    }
    if(n.includes("deep researcher"))return "Research-first agent for multi-source investigations, citations, and synthesized reports.";
    if(n.includes("daedalus"))return "Agentic coding workflow for greenfield builds and existing projects: plans, builds, repairs, reviews, and delivers verified code.";
    if(n.includes("coder"))return "General coding agent for shell work, file edits, debugging, and implementation help.";
    if(n.includes("conspiracy"))return "Investigative research agent for hidden narratives, disputed claims, source comparison, and deep-dive synthesis.";
    const tools=(mc.tool_ids||[]);
    const parts=[];
    if(tools.includes("codeagent"))parts.push("code execution");
    if(tools.includes("deep_research"))parts.push("agent research");
    if(tools.includes("conspiracy_research"))parts.push("investigative research");
    if(tools.includes("quick_search"))parts.push("quick search");
    if((mc.kb_ids||[]).length)parts.push(`${(mc.kb_ids||[]).length} knowledge base${(mc.kb_ids||[]).length===1?"":"s"}`);
    return parts.length?`Custom agent with ${parts.join(", ")} enabled.`:"Custom agent with its own model, prompt, tools, and knowledge context.";
  };
  const getProfileForConversation=(c)=>c?.model_config_id?mcs.find(mc=>mc.id===c.model_config_id):(c?.persona_name?mcs.find(mc=>mc.name===c.persona_name):null);
  const getConversationProfileType=(c)=>{
    const mc=getProfileForConversation(c);
    if(mc)return getProfileType(mc);
    if(isCoderPersonaName(c?.persona_name)||(c?.tool_ids||[]).length)return "agent";
    return "persona";
  };
  const replacePersonaPlaceholders=(text,names={})=>{
    if(text===null||text===undefined)return text;
    const userName=String(names.userName||currentUser?.name||"User").trim()||"User";
    const charName=String(names.charName||"the character").trim()||"the character";
    return String(text).replace(/\{\{\s*(user|char)\s*\}\}|\{\s*(user|char)\s*\}/gi,(_,a,b)=>{
      const key=String(a||b||"").toLowerCase();
      return key==="user"?userName:key==="char"?charName:_;
    });
  };
  const personaPlaceholderContextForPayload=(payload,mc=null)=>{
    const profile=mc||(payload?.model_config_id?mcs.find(x=>x.id===payload.model_config_id):null);
    const type=profile?getProfileType(profile):(payload?.persona_name?"persona":null);
    if(type!=="persona")return null;
    return {userName:currentUser?.name||"User",charName:payload?.persona_name||profile?.name||"the character"};
  };
  const personaPlaceholderContextForConversation=(c)=>{
    if(!c?.persona_name)return null;
    if(getConversationProfileType(c)!=="persona")return null;
    return {userName:currentUser?.name||"User",charName:c.persona_name};
  };
  const replacePersonaPlaceholdersForConversation=(text,c)=>{
    const ctx=personaPlaceholderContextForConversation(c);
    return ctx?replacePersonaPlaceholders(text,ctx):text;
  };
  const updateProfileLocal=(id,fn)=>setMcs(p=>p.map(mc=>mc.id===id?fn(mc):mc));
  const setProfileParam=(id,key,value)=>updateProfileLocal(id,mc=>({...mc,parameters:{...normalizeProfileParams(mc),[key]:value}}));
  const setPersonaField=(id,key,value)=>updateProfileLocal(id,mc=>{
    const params=normalizeProfileParams({...mc,parameters:{...(mc.parameters||{}),profile_type:"persona"}});
    const persona={...(params.persona||{}),[key]:key==="tags"?parseTags(value):value};
    const nextParams={...params,profile_type:"persona",persona,description:persona.description||""};
    return {...mc,parameters:nextParams,system_prompt:buildPersonaPrompt({name:mc.name,...persona})};
  });
  // Quick search — derived from active conversation tool_ids, or pending empty-chat selections.
  const activeToolIds=act?(act.tool_ids||[]):pendingToolIds;
  const quickSearch = activeToolIds.includes("quick_search");
  const activeMemoryWs=(wsDetail&&wsDetail.id&&Array.isArray(wsDetail.conversations)&&actId&&wsDetail.conversations.some(wc=>wc.id===actId))?wsDetail:null;
  const workspaceMemoryActive=!ghostActive&&activeMemoryWs&&(activeMemoryWs.memory_enabled==="1"||activeMemoryWs.memory_enabled===1||activeMemoryWs.memory_enabled===true);
  const globalMemoryActive=!ghostActive&&(act?(act.use_memories==="1"||act.use_memories===1||act.use_memories===true):pendingUseMemories);
  const toggleGlobalMemory=()=>{
    if(ghostActive)return;
    const next=globalMemoryActive?"0":"1";
    if(!actId||!act){
      setPendingUseMemories(next==="1");
      notify({type:next==="1"?"success":"info",text:next==="1"?"Memory enabled for next chat":"Memory disabled for next chat",duration:2200});
      return;
    }
    uConv(actId,{use_memories:next});
    notify({type:next==="1"?"success":"info",text:next==="1"?"Memory enabled for this chat":"Memory disabled for this chat",duration:2200});
  };
  const storedLastModel=()=>{try{return localStorage.getItem("hc-last-model")||"";}catch{return "";}};
  const normalizeAvailableModel=(modelName)=>{
    const chosen=modelName||"";
    if(chosen&&models.length>0&&!models.includes(chosen))return models[0]||"";
    return chosen;
  };
  const pendingChatModel=normalizeAvailableModel(pendingPersona?.model||pendingModel||storedLastModel()||models[0]||"");
  const setPendingChatModel=(modelName)=>{
    const chosen=normalizeAvailableModel(modelName)||modelName||"";
    modelChoiceRef.current.pending=chosen;
    setPendingModel(chosen);
    if(chosen)try{localStorage.setItem("hc-last-model",chosen);}catch{}
    setPendingPersona(p=>p?{...p,model:chosen}:p);
  };

  // Save helpers — persist to backend
  const saveTool=(tl)=>{fetch(`${API}/api/tools/${tl.id}`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:tl.name,description:tl.description,code:tl.code})}).catch(()=>{});};
  const saveProfile=async(mc)=>{
    const parameters=normalizeProfileParams(mc);
    setMcs(p=>p.map(x=>x.id===mc.id?{...x,parameters}:x));
    return fetch(`${API}/api/model-configs/${mc.id}`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:mc.name,base_model:mc.base_model,system_prompt:mc.system_prompt,tool_ids:mc.tool_ids||[],kb_ids:mc.kb_ids||[],parameters})}).catch(()=>{});
  };
  const profileFirstMessage=(payload)=>{
    const mc=payload?.model_config_id?mcs.find(x=>x.id===payload.model_config_id):null;
    if(!mc)return"";
    const params=normalizeProfileParams(mc);
    if(params.profile_type!=="persona")return"";
    const raw=String(params.persona?.first_message||"").trim();
    const ctx=personaPlaceholderContextForPayload({...payload,persona_name:payload?.persona_name||mc.name},mc);
    return ctx?replacePersonaPlaceholders(raw,ctx):raw;
  };
  const makeProfileFirstMessage=(payload)=>{
    const content=profileFirstMessage(payload);
    if(!content)return null;
    return {role:"assistant",content,metadata:{persona_first_message:true,model_config_id:payload.model_config_id||null},created_at:new Date().toISOString()};
  };
  const persistProfileFirstMessage=async(convId,payload)=>{
    const msg=makeProfileFirstMessage(payload);
    if(!msg)return null;
    if(String(convId||"").startsWith("ghost-"))return msg;
    try{
      const r=await fetch(`${API}/api/conversations/${convId}/messages`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({role:"assistant",content:msg.content,metadata:msg.metadata})});
      const d=await r.json().catch(()=>({}));
      if(d.message_id)msg.id=d.message_id;
    }catch(e){console.warn("Could not persist persona first message:",e);}
    return msg;
  };
  const applyProfile=async(mc)=>{
    const parameters=normalizeProfileParams(mc);
    const payload={model:mc.base_model||act?.model||models[0]||"",system_prompt:mc.system_prompt||"",tool_ids:mc.tool_ids||[],model_config_id:mc.id,persona_name:mc.name,persona_avatar:profileAvatar({...mc,parameters})};
    setMcs(p=>p.map(x=>x.id===mc.id?{...x,parameters}:x));
    await saveProfile({...mc,parameters});
    if(actId){
      uConv(actId,payload);
      if(!(act?.messages||[]).length){
        const firstMsg=await persistProfileFirstMessage(actId,payload);
        if(firstMsg)uConv(actId,c=>({...c,messages:[...(c.messages||[]),firstMsg]}));
      }
    }else if(profileFirstMessage(payload)){
      setPendingPersona(null);setPendingToolIds([]);
      await newChat(true,{personaOverride:payload});
    }else{const pendingProfileModel=payload.model||pendingChatModel||"";modelChoiceRef.current.pending=pendingProfileModel;setPendingPersona(payload);setPendingToolIds(payload.tool_ids||[]);setPendingModel(pendingProfileModel);}
    setLastPersonaId(mc.id);localStorage.setItem("hc-last-persona",mc.id);setPanel("chat");
  };
  const clearPendingProfile=()=>{setPendingPersona(null);setPendingToolIds([]);setLastPersonaId(null);try{localStorage.removeItem("hc-last-persona");}catch{};};
  const saveKb=(kb)=>{
    if(!kb?.id)return Promise.resolve();
    return fetch(`${API}/api/knowledge-bases/${kb.id}`,{
      method:"PUT",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name:kb.name||"",description:kb.description||""})
    }).catch(()=>{});
  };
  const scheduleKbSave=(kb,delay=450)=>{
    if(!kb?.id)return;
    const timers=kbSaveTimersRef.current;
    if(timers.has(kb.id))clearTimeout(timers.get(kb.id));
    timers.set(kb.id,setTimeout(()=>{timers.delete(kb.id);saveKb(kb);},delay));
  };
  const flushKbSave=(kb)=>{
    if(!kb?.id)return;
    const timers=kbSaveTimersRef.current;
    if(timers.has(kb.id)){clearTimeout(timers.get(kb.id));timers.delete(kb.id);}
    saveKb(kb);
  };
  const parseConnectorJson=(txt,fallback,label)=>{
    try{return (txt||"").trim()?JSON.parse(txt):fallback;}catch(e){notify({type:"error",text:`Invalid ${label}`,detail:e.message||String(e)});throw e;}
  };
  const refreshConnectors=async()=>{
    try{
      const [mcp,oa,ct]=await Promise.all([
        fetch(`${API}/api/mcp-servers`).then(r=>r.ok?r.json():[]),
        fetch(`${API}/api/openapi-connectors`).then(r=>r.ok?r.json():[]),
        fetch(`${API}/api/connector-tools`).then(r=>r.ok?r.json():[]),
      ]);
      setMcpServers(Array.isArray(mcp)?mcp:[]);
      setOpenapiConnectors(Array.isArray(oa)?oa:[]);
      setConnectorTools(Array.isArray(ct)?ct:[]);
    }catch(e){console.error("Connectors:",e);}
  };
  const discoverConnector=async(kind,id)=>{
    setConnectorBusy(`${kind}:${id}`);
    try{
      const path=kind==="mcp"?`mcp-servers/${id}`:`openapi-connectors/${id}`;
      const r=await fetch(`${API}/api/${path}/discover`,{method:"POST"});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      notify({type:"success",text:"Connector discovered",detail:`${d.tool_count||0} tools available`});
      await refreshConnectors();
    }catch(e){notify({type:"error",text:"Connector discovery failed",detail:e.message||String(e)});}
    finally{setConnectorBusy("");}
  };

  // Combined tool list for agents/personas (builtin + custom uploaded tools)
  const allTools = (() => {
    const seen = new Set();
    const merged = [];
    // builtinTools already includes custom tools from /api/builtin-tools
    for (const tl of builtinTools) { if (!seen.has(tl.id)) { seen.add(tl.id); merged.push(tl); } }
    for (const tl of connectorTools) { if (!seen.has(tl.id)) { seen.add(tl.id); merged.push({id:tl.id,name:tl.name||tl.display_name,description:tl.description||tl.external_name,icon:"plug",connector:true}); } }
    // Only add from tools DB if not already present
    for (const tl of tools) { if (!seen.has(tl.id)) { seen.add(tl.id); merged.push({id:tl.id, name:tl.name, description:tl.description||tl.filename, icon:"code"}); } }
    return merged;
  })();
  const connectorOperationTools=allTools.filter(tl=>tl.connector);
  const activeConnectorTools=connectorOperationTools.filter(tl=>activeToolIds.includes(tl.id));
  const activeConnectorCount=activeConnectorTools.length;
  const composerToolChips=allTools.filter(tl=>!tl.connector).concat(activeConnectorTools.slice(0,3));
  const filteredConnectorTools=connectorOperationTools.filter(tl=>{
    const q=connectorSearch.trim().toLowerCase();
    if(!q)return true;
    return String(tl.name||"").toLowerCase().includes(q)||String(tl.description||"").toLowerCase().includes(q)||String(tl.id||"").toLowerCase().includes(q);
  });
  const toggleToolId=(toolId)=>{
    if(!toolId)return;
    if(!actId){
      setPendingToolIds(ids=>ids.includes(toolId)?ids.filter(x=>x!==toolId):[...ids,toolId]);
      return;
    }
    uConv(actId,c=>{const ids=c.tool_ids||[];return{...c,tool_ids:ids.includes(toolId)?ids.filter(x=>x!==toolId):[...ids,toolId]};});
  };

  const resetUserScopedState=()=>{
    setConvs([]);setVisibleMessageCounts({});setActId(null);setEvts([]);evtsRef.current=[];streamSaveEvtsRef.current=[];
    setKbs([]);setTools([]);setMcs([]);setWorkspaces([]);setActiveWs(null);setWsDetail(null);setWsPanel(false);
    setConnectorTools([]);setMcpServers([]);setOpenapiConnectors([]);
    setResearchReports([]);setActiveResearchId(null);setActiveResearch(null);setResearchEvents([]);setResearchLiveMarkdown("");setResearchRunning(false);
    setCouncils([]);setActiveCouncilId(null);setCouncilReport(null);setQuickResults([]);setSearchLoading(false);setQuickSearchError(null);
    setCoderWorkflows([]);setCoderProjInfo(null);setAttachments([]);setFtsResults([]);setArtifactFocusId(null);setPanel("chat");setLoadingConv(false);
  };
  const refreshUsers=useCallback(async()=>{
    const r=await fetch(`${API}/api/users`);
    if(!r.ok)throw new Error(`HTTP ${r.status}`);
    const d=await r.json();
    const list=d.users||[];
    setUsers(list);
    setUserNameDrafts(p=>{const next={...p};list.forEach(u=>{if(next[u.id]===undefined)next[u.id]=u.name||"";});return next;});
    if(!list.some(u=>u.id===loginUserId))setLoginUserId(list[0]?.id||"default");
    return list;
  },[loginUserId]);
  const applySignedInUser=(user,sessionToken=undefined)=>{
    if(!user)return;
    const uid=user.id||"default";
    const effectiveSession=sessionToken!==undefined?(sessionToken||""):hcStoredSession(uid);
    hcSetUserContext(uid,effectiveSession);
    try{
      localStorage.setItem(HC_USER_KEY,uid);
      if(sessionToken!==undefined){
        if(sessionToken)localStorage.setItem(hcSessionKey(uid),sessionToken);
        else localStorage.removeItem(hcSessionKey(uid));
      }
    }catch{}
    resetUserScopedState();
    setCurrentUser(user);setCurrentUserId(uid);setLoginUserId(uid);setLoginPassword("");setLoginError("");setUserGateOpen(false);setAuthReady(true);
  };
  useEffect(()=>{
    let cancelled=false;
    const gate=(list=[],msg="")=>{
      if(cancelled)return;
      setUsers(list);setAuthReady(false);setUserGateOpen(true);setLoginError(msg);setLoginPassword("");
      if(list.length&&!list.some(u=>u.id===loginUserId))setLoginUserId(list[0].id);
      dismissHyprLoader();
    };
    (async()=>{
      try{
        const list=await refreshUsers();
        if(cancelled)return;
        let rememberedRaw="";
        try{rememberedRaw=localStorage.getItem(HC_USER_KEY)||"";}catch{}
        const remembered=rememberedRaw?(list.find(u=>u.id===rememberedRaw)||null):null;
        if(!remembered){if(list[0])setLoginUserId(list[0].id);gate(list,"");return;}
        if(remembered.password_enabled){
          hcSetUserContext(remembered.id,hcStoredSession(remembered.id));
          const r=await fetch(`${API}/api/users/current`);
          if(!r.ok){gate(list,"Enter the password for the remembered user.");return;}
          const d=await r.json();
          if(!cancelled)applySignedInUser(d.user||remembered);
        }else if(!cancelled){
          applySignedInUser(remembered,null);
        }
      }catch(e){
        gate([],e.message||"Could not load users.");
      }
    })();
    return()=>{cancelled=true;};
  },[]);
  const loginAsUser=async(userId=loginUserId,password=loginPassword)=>{
    setLoginError("");
    try{
      const r=await fetch(`${API}/api/users/login`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({user_id:userId,password:password||""})});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      const user=d.user||users.find(u=>u.id===userId);
      applySignedInUser(user,d.session_token===undefined?undefined:d.session_token);
      await refreshUsers().catch(()=>{});
      notify({type:"success",text:`Signed in as ${user?.name||userId}`,duration:2400});
    }catch(e){
      setLoginError(e.message||"Sign in failed");
      notify({type:"error",text:"Sign in failed",detail:e.message||String(e)});
    }
  };
  const logoutCurrentUser=async()=>{
    const uid=currentUserId||hcStoredUserId();
    try{await fetch(`${API}/api/users/logout`,{method:"POST"});}catch{}
    try{localStorage.removeItem(HC_USER_KEY);localStorage.removeItem(hcSessionKey(uid));}catch{}
    hcSetUserContext("default","");
    resetUserScopedState();setCurrentUser(null);setAuthReady(false);setLoginPassword("");setLoginError("");setUserGateOpen(true);
    await refreshUsers().catch(()=>{});
    dismissHyprLoader();
  };
  const createManagedUser=async()=>{
    const name=newUserName.trim();
    if(!name)return;
    setLoginError("");
    try{
      const password=newUserPassword;
      const r=await fetch(`${API}/api/users`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,password})});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      setNewUserName("");setNewUserPassword("");
      await refreshUsers().catch(()=>{});
      await loginAsUser(d.user.id,password);
    }catch(e){
      setLoginError(e.message||"Could not create user");
      notify({type:"error",text:"User create failed",detail:e.message||String(e)});
    }
  };
  const updateManagedUser=async(userId,patch)=>{
    try{
      const r=await fetch(`${API}/api/users/${encodeURIComponent(userId)}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(patch)});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      setUsers(p=>p.map(u=>u.id===userId?d.user:u));
      setUserNameDrafts(p=>({...p,[userId]:d.user.name||""}));
      if(patch.password!==undefined||patch.clear_password)setUserPasswordDrafts(p=>({...p,[userId]:""}));
      if(userId===currentUserId){
        applySignedInUser(d.user,d.session_token===undefined?undefined:d.session_token);
      }
      notify({type:"success",text:"User updated",duration:2200});
      await refreshUsers().catch(()=>{});
    }catch(e){
      notify({type:"error",text:"User update failed",detail:e.message||String(e)});
    }
  };
  const deleteManagedUser=async(user)=>{
    if(!user||user.id==="default")return;
    const ok=await confirmAction({title:"Delete user",body:`Delete ${user.name || user.id}? This removes that user's chats, workspaces, knowledge bases, tools, agents, memories, reports, and analytics.`,confirmLabel:"Delete User",tone:"danger"});
    if(!ok)return;
    try{
      const r=await fetch(`${API}/api/users/${encodeURIComponent(user.id)}`,{method:"DELETE"});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      try{localStorage.removeItem(hcSessionKey(user.id));if(currentUserId===user.id)localStorage.removeItem(HC_USER_KEY);}catch{}
      const list=await refreshUsers().catch(()=>users.filter(u=>u.id!==user.id));
      if(currentUserId===user.id){
        resetUserScopedState();setCurrentUser(null);setAuthReady(false);setUserGateOpen(true);setLoginUserId((list||[])[0]?.id||"default");
      }
      notify({type:"success",text:"User deleted",duration:2400});
    }catch(e){
      notify({type:"error",text:"User delete failed",detail:e.message||String(e)});
    }
  };

  // Load
  useEffect(()=>{
    if(!authReady){if(userGateOpen)dismissHyprLoader();return;}
    const dismissLoader=dismissHyprLoader;
    setTimeout(dismissLoader,5000); // fallback
    fetch(`${API}/api/models`).then(r=>r.json()).then(d=>{const next=d.models||[];setModels(next);setModelDetails(d.model_details||{});try{const last=localStorage.getItem("hc-last-model")||"";if(last&&next.length&&!next.includes(last))localStorage.setItem("hc-last-model",next[0]||"");}catch{}dismissLoader();}).catch(()=>{setModels(["qwen3.5:27b","qwen3-coder:30b"]);dismissLoader();});
    fetch(`${API}/api/conversations`).then(r=>r.json()).then(cs=>{if(cs.length){
      // Seed last-model from most recent conversation if not set
      try{if(!localStorage.getItem("hc-last-model")&&cs[0]?.model)localStorage.setItem("hc-last-model",cs[0].model);}catch{}
      const isCouncil=v=>v==="1"||v===1||v===true;
      setConvs(cs.map(c=>({...c, messages: [], is_council: isCouncil(c.is_council), council_config_id: c.council_config_id||null})));
      setActId(null);
      setPanel("chat");
      setLoadingConv(false);
    }}).catch(()=>{});
    fetch(`${API}/api/knowledge-bases`).then(r=>r.json()).then(setKbs).catch(()=>{});
    fetch(`${API}/api/tools`).then(r=>r.json()).then(setTools).catch(()=>{});
    refreshConnectors();
    fetch(`${API}/api/model-configs`).then(r=>r.json()).then(d=>setMcs((d||[]).map(mc=>({...mc,parameters:normalizeProfileParams(mc)})))).catch(()=>{});
    fetch(`${API}/api/health`).then(r=>r.json()).then(d=>setHealth(d.services||{})).catch(()=>{});
    fetch(`${API}/api/model-providers`).then(r=>r.json()).then(d=>{const map={};(d.providers||[]).forEach(p=>{map[p.provider]=p;});setModelProviders(map);}).catch(()=>{});
    fetch(`${API}/api/settings`).then(r=>r.json()).then(d=>{
      if(d.current_ollama_url)setOllamaUrl(d.current_ollama_url);
      if(d.current_codebox_url)setCodeboxUrl(d.current_codebox_url);
      if(d.current_searxng_url)setSearxngUrl(d.current_searxng_url);
      if(d.current_n8n_url)setN8nUrl(d.current_n8n_url);
      if(d.current_comfyui_url!==undefined)setComfyuiUrl(d.current_comfyui_url||"");
      if(d.current_stt_url!==undefined)setSttUrl(d.current_stt_url||"");
      if(d.current_tts_url!==undefined)setTtsUrl(d.current_tts_url||"");
      if(d.current_tts_voice)setTtsVoice(d.current_tts_voice);
      if(d.current_tts_url)fetch(`${API}/api/audio/voices`).then(r=>r.json()).then(v=>setTtsVoices(v.voices||[])).catch(()=>{});
      if(d.ollama_scan_ssh_host!==undefined)setOllamaScanSshHost(d.ollama_scan_ssh_host||"");
      if(d.ollama_scan_ssh_port!=null)setOllamaScanSshPort(Number(d.ollama_scan_ssh_port)||22);
      if(d.ollama_scan_ssh_user!==undefined)setOllamaScanSshUser(d.ollama_scan_ssh_user||"root");
      if(d.ollama_scan_ssh_auth_mode)setOllamaScanSshAuthMode(d.ollama_scan_ssh_auth_mode==="password"?"password":"key");
      if(d.ollama_scan_ssh_key_path!==undefined)setOllamaScanSshKeyPath(d.ollama_scan_ssh_key_path||"");
      setOllamaScanSshHasPassword(!!d.ollama_scan_ssh_has_password);
      setOllamaScanSshPassword("");
      setOllamaScanSshClearPassword(false);
      if(d.rag)setRagSettings(p=>({...p,...d.rag}));
      if(d.default_num_ctx!=null)hydrateServerSetting("default_num_ctx",setNumCtx,d.default_num_ctx,numCtx);
      if(d.current_planning_model!=null)hydrateServerSetting("planning_model",setPlanningModel,d.current_planning_model,planningModel);
      if(d.current_coder_model!=null)hydrateServerSetting("coder_model",setCoderModel,d.current_coder_model,coderModel);
      if(d.current_architect_model!=null)hydrateServerSetting("architect_model",setArchitectModel,d.current_architect_model,architectModel);
      if(d.current_reviewer_model!=null)hydrateServerSetting("reviewer_model",setReviewerModel,d.current_reviewer_model,reviewerModel);
      if(d.current_acceptance_model!=null)hydrateServerSetting("acceptance_model",setAcceptanceModel,d.current_acceptance_model,acceptanceModel);
      if(d.current_builder_model!=null)hydrateServerSetting("builder_model",setBuilderModel,d.current_builder_model,builderModel);
      if(d.current_fixer_model!=null)hydrateServerSetting("fixer_model",setFixerModel,d.current_fixer_model,fixerModel);
      if(d.current_qa_model!=null)hydrateServerSetting("qa_model",setQaModel,d.current_qa_model,qaModel);
      if(d.current_workspace_model!=null)hydrateServerSetting("workspace_model",setWsModel,d.current_workspace_model,wsModel);
      if(d.openhands_num_ctx!=null)hydrateServerSetting("openhands_num_ctx",setCoderNumCtx,d.openhands_num_ctx,coderNumCtx);
      if(d.research_num_ctx!=null)hydrateServerSetting("research_num_ctx",setResearchNumCtx,d.research_num_ctx,researchNumCtx);
      if(d.openhands_enabled!=null)hydrateServerSetting("openhands_enabled",setOpenhandsEnabled,d.openhands_enabled,openhandsEnabled);
      if(d.openhands_max_rounds!=null)hydrateServerSetting("openhands_max_rounds",setOpenhandsMaxRounds,d.openhands_max_rounds,openhandsMaxRounds);
      if(d.openhands_reasoning_effort)hydrateServerSetting("openhands_reasoning_effort",setOpenhandsReasoningEffort,d.openhands_reasoning_effort,openhandsReasoningEffort);
      if(d.openhands_disable_thinking!=null)hydrateServerSetting("openhands_disable_thinking",setOpenhandsDisableThinking,d.openhands_disable_thinking,openhandsDisableThinking);
      if(d.aider_enabled!=null)hydrateServerSetting("aider_enabled",setAiderEnabled,d.aider_enabled,aiderEnabled);
      if(d.aider_model!=null)hydrateServerSetting("aider_model",setAiderModel,d.aider_model,aiderModel);
      if(d.aider_auto_test!=null)hydrateServerSetting("aider_auto_test",setAiderAutoTest,d.aider_auto_test,aiderAutoTest);
      if(d.quick_search_mode)hydrateServerSetting("quick_search_mode",setQuickSearchMode,d.quick_search_mode,quickSearchMode);
      if(d.image_chat_checkpoint!=null)hydrateServerSetting("image_chat_checkpoint",setImgChatCkpt,d.image_chat_checkpoint,imgChatCkpt);
      if(d.image_chat_resolution)hydrateServerSetting("image_chat_resolution",setImgChatRes,d.image_chat_resolution,imgChatRes);
      if(d.image_chat_vae!=null)hydrateServerSetting("image_chat_vae",setImgChatVae,d.image_chat_vae,imgChatVae);
      if(d.image_chat_workflow!=null)hydrateServerSetting("image_chat_workflow",setImgChatWorkflow,d.image_chat_workflow,imgChatWorkflow);
      if(d.image_chat_prompt_prefix!=null)setImgChatPrefix(d.image_chat_prompt_prefix);
      if(d.image_chat_negative!=null)setImgChatNeg(d.image_chat_negative);
      if(d.image_chat_compose_model!=null)hydrateServerSetting("image_chat_compose_model",setImgChatComposeModel,d.image_chat_compose_model,imgChatComposeModel);
      if(d.model_hardware_profile)setHyprfitProfile(d.model_hardware_profile);
      settingsLoadedRef.current=true;
    }).catch(()=>{settingsLoadedRef.current=true;});
    fetch(`${API}/api/rag/stats`).then(r=>r.json()).then(setRagStats).catch(()=>{});
    fetch(`${API}/api/workspaces`).then(r=>r.json()).then(setWorkspaces).catch(()=>{});
    fetch(`${API}/api/research/templates`).then(r=>r.json()).then(d=>{const tm=d.templates||[];setResearchTemplates(tm);if(tm[0])setResearchDraft(p=>({...p,report_type:p.report_type||tm[0].id,depth:p.depth||tm[0].default_depth||3}));}).catch(()=>{});
    fetch(`${API}/api/research/reports`).then(r=>r.json()).then(d=>{setResearchReports(d||[]);if((d||[])[0]){setActiveResearchId((d||[])[0].id);setResearchView("reports");}}).catch(()=>{});
    fetch(`${API}/api/councils`).then(r=>r.json()).then(setCouncils).catch(()=>{});
    fetch(`${API}/api/council-presets`).then(r=>r.json()).then(setCouncilPresets).catch(()=>{});
    const quickSearchEntry={id:"quick_search",name:"⚡ Quick Search",description:"SearXNG instant search with inline YouTube, image & web previews",icon:"flash"};
    fetch(`${API}/api/builtin-tools`).then(r=>r.json()).then(d=>{if(Array.isArray(d))setBuiltinTools([...d,quickSearchEntry]);}).catch(()=>{
      setBuiltinTools([
        {id:"codeagent",name:"⚡ CodeAgent",description:"Code execution, shell, file management, downloads",icon:"cpu"},
        {id:"deep_research",name:"🔬 Agent Research",description:"Agent-focused web research for current APIs, coding blockers, repeated errors, and concise implementation guidance",icon:"search"},
        quickSearchEntry,
      ]);
    });
  },[authReady,currentUserId]);

  useEffect(()=>{
    if(authReady&&panel==="models"&&modelsTab==="hyprfit")loadHyprfit();
  },[authReady,panel,modelsTab]);

  // SSE — with exponential backoff reconnection
  useEffect(()=>{
    if(sseR.current)sseR.current.close();
    if(!actId)return;
    let closed=false,retryDelay=1000,retryTimer=null;
    const connect=()=>{
      if(closed)return;
      const es=new EventSource(userScopedUrl(`/api/events/${actId}`));
      es.onmessage=e=>{try{const ev=JSON.parse(e.data);if(ev.type!=="heartbeat"&&ev.type!=="keepalive"){setEvts(p=>[...p.slice(-200),ev]);streamSaveEvtsRef.current.push(ev);
        // Route search_agent results into the QUICK SEARCH carousel — same data
        // the chat model saw, instead of the old parallel /api/quick-search call.
        if(ev.type==="search_results"&&ev.data?.source==="quick_search"){
          const results=ev.data.results||[];
          setQuickResults(results);
          setSearchLoading(false);
          setQuickSearchError(ev.data.error||ev.data.failed||null);
        }
        if(["tool_done","tool_end","tool_error"].includes(ev.type)&&ev.data?.tool==="quick_search"){
          const status=String(ev.data?.status||"");
          setSearchLoading(false);
          if(ev.type==="tool_error"||/failed|unavailable|error/i.test(status))setQuickSearchError(status||"Search failed");
          else setQuickSearchError(null);
        }
        }retryDelay=1000;}catch{}};
      es.onerror=()=>{es.close();if(!closed){retryTimer=setTimeout(()=>{retryDelay=Math.min(retryDelay*2,30000);connect();},retryDelay);}};
      sseR.current=es;
    };
    connect();
    return()=>{closed=true;if(retryTimer)clearTimeout(retryTimer);sseR.current?.close();};
  },[actId]);
  useEffect(()=>{if(actId)refreshCoderWorkflows(actId);},[actId,refreshCoderWorkflows]);
  useEffect(()=>{
    if(!searchLoading)return;
    const id=setTimeout(()=>{setSearchLoading(false);setQuickSearchError("No results returned before the timeout.");},25000);
    return()=>clearTimeout(id);
  },[searchLoading]);
  useEffect(()=>{
    if(!actId||!evts.some(e=>e.data?.workflow_id||e.type==="tool_end"||e.type==="tool_progress"))return;
    const id=setTimeout(()=>refreshCoderWorkflows(actId),500);
    return()=>clearTimeout(id);
  },[evts,actId,refreshCoderWorkflows]);

  // Auto-expand thinking pill when enabled
  useEffect(()=>{
    if(autoExpandThink&&evts.some(e=>e.type==="thinking")){setExpandedPill("thinking");}
  },[evts,autoExpandThink]);
  useEffect(()=>{evtsRef.current=evts;},[evts]);

  const refreshResearchReports=useCallback(async(q=researchReportFilter)=>{
    try{
      const r=await fetch(`${API}/api/research/reports${q?`?q=${encodeURIComponent(q)}`:""}`);
      if(r.ok)setResearchReports(await r.json());
    }catch(e){console.error("Research report refresh failed",e);}
  },[researchReportFilter]);

  const loadResearchReport=useCallback(async(id,{openPanel=true}={})=>{
    if(!id)return;
    setResearchLoading(true);
    try{
      const r=await fetch(`${API}/api/research/reports/${id}`);
      if(!r.ok)throw new Error(`HTTP ${r.status}`);
      const d=await r.json();
      setActiveResearchId(id);setActiveResearch(d);
      setResearchEvents(d.events_log||[]);
      setResearchLiveMarkdown(cleanResearchMarkdown(d.report_markdown||""));
      setResearchRunning(["queued","running"].includes(d.status));
      if(openPanel)setPanel("research");
    }catch(e){console.error("Research report load failed",e);notify({type:"error",text:"Research report failed to load",detail:e.message||String(e)});}
    setResearchLoading(false);
  },[notify]);

  useEffect(()=>{if(activeResearchId)loadResearchReport(activeResearchId,{openPanel:false});},[activeResearchId]);

  useEffect(()=>{
    if(researchEventRef.current){researchEventRef.current.close();researchEventRef.current=null;}
    if(!activeResearchId||!researchRunning)return;
    let closed=false;
    const es=new EventSource(userScopedUrl(`/api/events/${activeResearchId}`));
    es.onmessage=e=>{try{
      const ev=JSON.parse(e.data);
      if(ev.type==="heartbeat"||ev.type==="keepalive")return;
      setResearchEvents(p=>[...p.slice(-300),ev]);
      if(ev.type==="research_token"&&ev.data?.content)setResearchLiveMarkdown(p=>p+ev.data.content);
      if(ev.type==="research_done"||ev.type==="research_error"){
        setResearchRunning(false);
        setTimeout(()=>{refreshResearchReports();loadResearchReport(activeResearchId,{openPanel:false});},600);
      }
    }catch{}};
    es.onerror=()=>{if(!closed)console.warn("Research event stream disconnected");};
    researchEventRef.current=es;
    return()=>{closed=true;es.close();if(researchEventRef.current===es)researchEventRef.current=null;};
  },[activeResearchId,researchRunning,refreshResearchReports,loadResearchReport]);

  useEffect(()=>{
    if(!activeResearchId||!researchRunning)return;
    let closed=false,timer=null;
    const sync=async()=>{
      try{
        const r=await fetch(`${API}/api/research/reports/${activeResearchId}`);
        if(!r.ok)return;
        const d=await r.json();
        if(closed)return;
        setActiveResearch(d);
        setResearchEvents(d.events_log||[]);
        if(d.report_markdown)setResearchLiveMarkdown(cleanResearchMarkdown(d.report_markdown||""));
        setResearchReports(p=>{
          const rest=p.filter(x=>x.id!==d.id);
          return [d,...rest];
        });
        if(["complete","failed","cancelled"].includes(String(d.status||"").toLowerCase())){
          setResearchRunning(false);
          refreshResearchReports();
        }
      }catch(e){console.warn("Research report poll failed",e);}
    };
    timer=setInterval(sync,5000);
    const first=setTimeout(sync,2500);
    return()=>{closed=true;clearInterval(timer);clearTimeout(first);};
  },[activeResearchId,researchRunning,refreshResearchReports]);

  const startResearchReport=async()=>{
    const query=(researchDraft.query||"").trim();
    if(!query){notify({type:"warning",text:"Research topic required",detail:"Enter a topic or question first."});return;}
    const notes=(researchInputText||"").trim();
    const inputs=[...(researchDraft.inputs||[])];
    if(notes)inputs.push({name:"Pasted notes",type:"notes",content:notes});
    const body={...researchDraft,query,depth:parseInt(researchDraft.depth)||3,model:researchDraft.model||researchSelectableModels[0]||models[0]||"",inputs};
    setResearchRunning(true);setResearchEvents([]);setResearchLiveMarkdown("");setResearchLoading(true);
    try{
      const r=await fetch(`${API}/api/research/reports`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      if(!r.ok){const txt=await r.text().catch(()=>r.statusText);throw new Error(`HTTP ${r.status}: ${txt}`);}
      const d=await r.json();
      setActiveResearchId(d.id);setActiveResearch(d);setResearchEvents(d.events_log||[]);
      setResearchReports(p=>[d,...p.filter(x=>x.id!==d.id)]);
      setResearchDraft(p=>({...p,query:"",focus:"",inputs:[]}));
      setResearchInputText("");
      setPanel("research");
      setResearchView("reports");
    }catch(e){setResearchRunning(false);notify({type:"error",text:"Research failed to start",detail:e.message||String(e)});}
    setResearchLoading(false);
  };

  const cancelResearchReport=async(id=activeResearchId)=>{
    if(!id)return;
    try{await fetch(`${API}/api/research/reports/${id}/cancel`,{method:"POST"});}catch{}
    setResearchRunning(false);
    setResearchReports(p=>p.map(r=>r.id===id?{...r,status:"cancelled",error:"Cancelled by user"}:r));
    if(activeResearchId===id)setActiveResearch(p=>p?{...p,status:"cancelled",error:"Cancelled by user"}:p);
  };

  const rerunResearchReport=async(id=activeResearchId)=>{
    if(!id)return;
    setResearchRunning(true);setResearchEvents([]);setResearchLiveMarkdown("");
    try{
      const r=await fetch(`${API}/api/research/reports/${id}/rerun`,{method:"POST"});
      if(!r.ok)throw new Error(`HTTP ${r.status}`);
      const d=await r.json();
      setActiveResearchId(d.id);setActiveResearch(d);setResearchEvents(d.events_log||[]);setResearchReports(p=>[d,...p.filter(x=>x.id!==d.id)]);setPanel("research");
    }catch(e){setResearchRunning(false);notify({type:"error",text:"Research rerun failed",detail:e.message||String(e)});}
  };

  const deleteResearchReport=async(id)=>{
    if(!id)return;
    const ok=await confirmAction({title:"Delete research report?",body:"This removes the saved report and source metadata.",confirmLabel:"Delete",tone:"danger"});
    if(!ok)return;
    try{await fetch(`${API}/api/research/reports/${id}`,{method:"DELETE"});}catch{}
    setResearchReports(p=>p.filter(r=>r.id!==id));
    if(activeResearchId===id){setActiveResearchId(null);setActiveResearch(null);setResearchEvents([]);setResearchLiveMarkdown("");setResearchRunning(false);}
  };

  const addResearchReportToWorkspace=async(reportId,wsId)=>{
    if(!reportId||!wsId)return;
    const ws=workspaces.find(w=>w.id===wsId);
    try{
      const r=await fetch(`${API}/api/workspaces/${wsId}/research-reports`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({report_id:reportId})});
      if(!r.ok)throw new Error(`HTTP ${r.status}`);
      const wl=await fetch(`${API}/api/workspaces`).then(r=>r.json()).catch(()=>null);
      if(wl)setWorkspaces(wl);
      if(activeWs===wsId){
        const d=await fetch(`${API}/api/workspaces/${wsId}`).then(r=>r.json()).catch(()=>null);
        if(d)setWsDetail(d);
      }
      notify({type:"success",text:"Report added to workspace",detail:ws?.name||""});
    }catch(e){notify({type:"error",text:"Could not add report to workspace",detail:e.message||String(e)});}
  };

  const removeResearchReportFromWorkspace=async(wsId,reportId)=>{
    if(!wsId||!reportId)return;
    try{
      await fetch(`${API}/api/workspaces/${wsId}/research-reports/${reportId}`,{method:"DELETE"});
      setWsDetail(d=>d&&d.id===wsId?{...d,reports:(d.reports||[]).filter(r=>r.id!==reportId)}:d);
      const wl=await fetch(`${API}/api/workspaces`).then(r=>r.json()).catch(()=>null);
      if(wl)setWorkspaces(wl);
    }catch(e){notify({type:"error",text:"Could not remove report",detail:e.message||String(e)});}
  };

  const handleResearchFiles=async(files)=>{
    const added=[];
    for(const f of Array.from(files||[])){
      const isPdf=(f.name||"").toLowerCase().endsWith(".pdf");
      if(f.size>25*1024*1024){notify({type:"warning",text:"Research file skipped",detail:`${f.name} is larger than 25MB.`});continue;}
      try{
        if(isPdf){
          const fd=new FormData();fd.append("file",f);
          const r=await fetch(`${API}/api/extract-pdf`,{method:"POST",body:fd});
          if(!r.ok)throw new Error(`PDF extraction HTTP ${r.status}`);
          const d=await r.json();
          added.push({name:f.name,type:"pdf",content:(d.text||"").slice(0,60000),pages:d.total_pages||0});
        }else{
          const text=await f.text();
          added.push({name:f.name,type:"file",content:text.slice(0,60000)});
        }
      }catch(e){notify({type:"error",text:"Research file failed",detail:`${f.name}: ${e.message||String(e)}`});}
    }
    if(added.length)setResearchDraft(p=>({...p,inputs:[...(p.inputs||[]),...added]}));
  };

  const researchExportName=(ext)=>{
    const report=activeResearch;
    const base=(report?.title||report?.query||"research-report")
      .replace(/[^a-z0-9._-]+/gi,"-")
      .replace(/^-+|-+$/g,"")
      .slice(0,80)||"research-report";
    return `${base}.${ext}`;
  };

  const escapePrintHtml=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  const researchPrintTheme={
    bgDeep:"#ffffff",
    surface:"#f8fafc",
    brd:"#d7dde8",
    acc:"#175cd3",
    ok:"#0f766e",
    warm:"#b7791f",
    err:"#b42318",
    text:"#161b22",
    dim:"#374151",
    mut:"#667085",
    sfHov:"#eef4ff"
  };
  const inlinePrintMarkdown=text=>{
    let out=escapePrintHtml(text);
    out=out.replace(/!\[([^\]]*)\]\(([^)]+)\)/g,(_m,alt,url)=>`<span class="image-note">Image: ${alt||url}</span>`);
    out=out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+|\/[^)\s]+)\)/g,(_m,label,url)=>`<a href="${url}" rel="noreferrer">${label}</a>`);
    out=out.replace(/`([^`]+)`/g,"<code>$1</code>");
    out=out.replace(/\*\*([^*\n]+)\*\*/g,"<strong>$1</strong>");
    out=out.replace(/\*([^*\n]+)\*/g,"<em>$1</em>");
    out=out.replace(/_([^_\n]+)_/g,"<em>$1</em>");
    return out;
  };

  const renderResearchMarkdownForPrint=markdown=>{
    const lines=String(markdown||"").trim().replace(/\r\n/g,"\n").split("\n");
    const html=[];let para=[],list=null,inCode=false,code=[];
    const flushPara=()=>{const txt=para.join(" ").trim();if(txt)html.push(`<p>${inlinePrintMarkdown(txt)}</p>`);para=[];};
    const flushList=()=>{if(list){html.push(`</${list}>`);list=null;}};
    const parseTableRow=r=>r.replace(/^\||\|$/g,"").split("|").map(c=>c.trim());
    for(let i=0;i<lines.length;i++){
      const ln=lines[i];
      if(/^```/.test(ln.trim())){
        if(inCode){html.push(`<pre><code>${escapePrintHtml(code.join("\n"))}</code></pre>`);code=[];inCode=false;}
        else{flushPara();flushList();inCode=true;code=[];}
        continue;
      }
      if(inCode){code.push(ln);continue;}
      if(!ln.trim()){flushPara();flushList();continue;}
      const h=ln.match(/^(#{1,6})\s+(.+)$/);
      if(h){flushPara();flushList();const lvl=Math.min(6,h[1].length);html.push(`<h${lvl}>${inlinePrintMarkdown(h[2])}</h${lvl}>`);continue;}
      if(/^---+$|^\*\*\*+$|^___+$/.test(ln.trim())){flushPara();flushList();html.push("<hr>");continue;}
      if(/^\|.+\|$/.test(ln.trim())){
        flushPara();flushList();
        const rows=[ln.trim()];let j=i+1;
        while(j<lines.length&&/^\|.+\|$/.test(lines[j].trim())){rows.push(lines[j].trim());j++;}
        i=j-1;
        const isSep=r=>r.replace(/[\s\-:|]/g,"").length===0&&r.includes("-");
        const parsed=rows.filter(r=>!isSep(r)).map(parseTableRow);
        const hasHeader=rows.length>1&&isSep(rows[1]);
        const head=hasHeader?parsed[0]:null;
        const body=hasHeader?parsed.slice(1):parsed;
        html.push("<table>");
        if(head)html.push(`<thead><tr>${head.map(c=>`<th>${inlinePrintMarkdown(c)}</th>`).join("")}</tr></thead>`);
        html.push(`<tbody>${body.map(r=>`<tr>${r.map(c=>`<td>${inlinePrintMarkdown(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
        continue;
      }
      const ul=ln.match(/^[-*+]\s+(.+)$/);
      if(ul){flushPara();if(list!=="ul"){flushList();list="ul";html.push("<ul>");}html.push(`<li>${inlinePrintMarkdown(ul[1])}</li>`);continue;}
      const ol=ln.match(/^\d+\.\s+(.+)$/);
      if(ol){flushPara();if(list!=="ol"){flushList();list="ol";html.push("<ol>");}html.push(`<li>${inlinePrintMarkdown(ol[1])}</li>`);continue;}
      const bq=ln.match(/^>\s?(.+)$/);
      if(bq){flushPara();flushList();html.push(`<blockquote>${inlinePrintMarkdown(bq[1])}</blockquote>`);continue;}
      para.push(ln.trim());
    }
    if(inCode)html.push(`<pre><code>${escapePrintHtml(code.join("\n"))}</code></pre>`);
    flushPara();flushList();
    return html.join("\n");
  };

  const researchPrintCss=()=>`
      *{box-sizing:border-box}
      body{margin:0;color:#161b22;background:#fff;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;font-size:10.8pt;line-height:1.58}
      main,.research-print-page{max-width:7.15in;margin:0 auto;background:#fff;color:#161b22}
      .report-cover{border-bottom:1px solid #d7dde8;margin:0 0 20pt;padding:0 0 14pt}
      .eyebrow{font-size:8.5pt;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:#57606a;margin:0 0 5pt}
      h1{font-size:21pt;line-height:1.14;margin:0 0 8pt;color:#111827;letter-spacing:0}
      .query{font-size:9.8pt;color:#4b5563;line-height:1.45;margin-top:6pt}
      .meta{display:flex;flex-wrap:wrap;gap:5pt;margin-top:10pt}
      .meta span{border:1px solid #d7dde8;border-radius:4pt;padding:2.5pt 5pt;color:#364152;background:#f8fafc;font-size:8.5pt}
      h2{font-size:14.5pt;line-height:1.25;margin:20pt 0 7pt;color:#1f2937;border-top:1px solid #d7dde8;padding-top:10pt}
      h3{font-size:12pt;line-height:1.3;margin:13pt 0 5pt;color:#1f2937}
      h4,h5,h6{font-size:10.5pt;line-height:1.3;margin:10pt 0 4pt;color:#374151}
      h1,h2,h3,h4{page-break-inside:avoid;break-inside:avoid;page-break-after:avoid;break-after:avoid}
      p{margin:0 0 8pt}
      p,li{orphans:2;widows:2}
      ul,ol{margin:0 0 9pt 18pt;padding:0}
      li{margin:0 0 4pt;padding-left:2pt}
      code{font-family:"SFMono-Regular",Menlo,Consolas,monospace;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:3pt;padding:0 3pt;color:#334155;font-size:.92em}
      pre,.print-code-block{white-space:pre-wrap;border:1px solid #d7dde8;background:#f8fafc;padding:8pt;border-radius:5pt;margin:9pt 0 11pt;page-break-inside:avoid;break-inside:avoid;color:#1f2937}
      pre code{border:0;background:transparent;padding:0;color:#1f2937}
      blockquote{border-left:3pt solid #94a3b8;margin:9pt 0;padding:4pt 0 4pt 10pt;color:#374151;background:#f8fafc}
      table,.print-md-table{width:100%;border-collapse:collapse;margin:9pt 0 12pt;page-break-inside:auto;break-inside:auto;table-layout:fixed;background:#fff}
      thead{display:table-header-group}
      tfoot{display:table-footer-group}
      tr{page-break-inside:avoid;break-inside:avoid;page-break-after:auto}
      th,td{border:1px solid #d7dde8;padding:5pt 6pt;text-align:left;vertical-align:top;font-size:8.8pt;line-height:1.35;overflow-wrap:anywhere;word-break:normal;color:#263238;background:#fff}
      th{background:#f1f5f9;color:#1f2937;font-weight:800}
      a{color:#175cd3;text-decoration:none;overflow-wrap:anywhere}
      hr{border:0;border-top:1px solid #d7dde8;margin:13pt 0}
      .image-note{display:inline-block;border:1px solid #d7dde8;border-radius:4pt;padding:1pt 4pt;color:#57606a;background:#f8fafc;font-size:.9em}
      .research-rendered-report{font-size:10.8pt;line-height:1.58;color:#161b22}
      .research-rendered-report>div{display:block}
      .research-rendered-report .mermaid-container{background:#fff!important;page-break-inside:avoid}
      .research-rendered-report .mermaid-container svg{max-width:100%;height:auto}
      .research-rendered-report canvas{max-width:100%}
      .research-rendered-report .katex-display{overflow-x:auto;overflow-y:hidden;page-break-inside:avoid}
      .chart-print-block,.mermaid-print-block,.research-rendered-report pre,.research-rendered-report blockquote,.research-rendered-report canvas{page-break-inside:avoid;break-inside:avoid}
      .print-details{border:1px solid #d7dde8;background:#f8fafc;border-radius:5pt;padding:7pt 9pt;margin:8pt 0}
      .print-details-summary{font-size:8.5pt;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:#475467;margin-bottom:4pt}
      .sources{margin-top:22pt;border-top:1px solid #d7dde8;padding-top:12pt}
      .sources li{margin-bottom:6pt;page-break-inside:avoid;break-inside:avoid}
      .source-title{font-weight:700;color:#1f2937}
      .source-url{font-size:8.5pt;color:#57606a;overflow-wrap:anywhere}
      @page{size:letter;margin:.58in}
      @media print{body{print-color-adjust:exact;-webkit-print-color-adjust:exact}.research-print-page{max-width:none}}
    `;

  const buildResearchPrintHtml=(report,markdown)=>{
    const bodyMd=String(markdown||"").trim().replace(/^#\s+.+(?:\n|$)/,"");
    const metrics=report?.metrics||{};
    const sources=report?.sources||[];
    const meta=[
      report?.report_type,
      report?.status,
      sources.length?`${sources.length} sources`:metrics.source_count?`${metrics.source_count} sources`:"",
      metrics.pages_read?`${metrics.pages_read} pages read`:"",
      metrics.elapsed?`${Math.round(metrics.elapsed)}s`:"",
      report?.model?`model ${report.model}`:"",
    ].filter(Boolean);
    const sourceHtml=sources.length?`<section class="sources"><h2>Sources</h2><ol>${sources.map(src=>{
      const idx=src.index||src.source_index||"";
      const title=escapePrintHtml(src.title||src.url||"Source");
      const url=escapePrintHtml(src.url||"");
      const snippet=escapePrintHtml(String(src.snippet||"").slice(0,600));
      return `<li><div class="source-title">${idx?`[S${idx}] `:""}${title}</div>${url?`<div class="source-url">${url}</div>`:""}${snippet?`<p>${snippet}</p>`:""}</li>`;
    }).join("")}</ol></section>`:"";
    return `<div class="research-print-page">
      <header class="report-cover">
        <div class="eyebrow">HyprChat Deep Research</div>
        <h1>${escapePrintHtml(report?.title||report?.query||"Research Report")}</h1>
        ${report?.query?`<div class="query">${escapePrintHtml(report.query)}</div>`:""}
        ${meta.length?`<div class="meta">${meta.map(x=>`<span>${escapePrintHtml(x)}</span>`).join("")}</div>`:""}
      </header>
      ${renderResearchMarkdownForPrint(bodyMd)}
      ${sourceHtml}
    </div>`;
  };

  const ResearchPrintPage=({report,markdown})=>{
    const bodyMd=String(markdown||"").trim().replace(/^#\s+.+(?:\n|$)/,"");
    const metrics=report?.metrics||{};
    const sources=report?.sources||[];
    const stamp=report?.completed_at||report?.updated_at||report?.created_at||new Date().toISOString();
    const meta=[
      report?.report_type&&`type ${report.report_type}`,
      report?.status&&`status ${report.status}`,
      sources.length?`${sources.length} sources`:metrics.source_count?`${metrics.source_count} sources`:"",
      metrics.pages_read?`${metrics.pages_read} pages read`:"",
      metrics.searches?`${metrics.searches} searches`:"",
      metrics.coverage_score!==undefined?`coverage ${metrics.coverage_score}/100`:"",
      stamp?new Date(stamp).toLocaleString():new Date().toLocaleString(),
    ].filter(Boolean);
    return <div className="research-print-page">
      <header className="report-cover">
        <div className="eyebrow">HyprChat Deep Research</div>
        <h1>{report?.title||report?.query||"Research Report"}</h1>
        {report?.query&&<div className="query">{report.query}</div>}
        {!!meta.length&&<div className="meta">{meta.map((x,i)=><span key={i}>{x}</span>)}</div>}
      </header>
      <section className="research-rendered-report markdown-print"><MDWrap>{md(bodyMd,{printMode:true,theme:researchPrintTheme})}</MDWrap></section>
      {!!sources.length&&<section className="sources">
        <h2>Sources</h2>
        <ol>{sources.map((src,i)=>{
          const idx=src.index||src.source_index||i+1;
          return <li key={idx+"-"+(src.url||i)}>
            <div className="source-title">[S{idx}] {src.title||src.url||"Source"}</div>
            {src.url&&<div className="source-url">{src.url}</div>}
            {src.credibility_score!==undefined&&<div className="source-url">Credibility: {src.credibility_score}/100{(src.credibility_factors||[]).length?` — ${(src.credibility_factors||[]).join(", ")}`:""}</div>}
            {src.snippet&&<p>{String(src.snippet).slice(0,600)}</p>}
          </li>;
        })}</ol>
      </section>}
    </div>;
  };

  const waitForResearchRender=async(host)=>{
    const sleep=ms=>new Promise(r=>setTimeout(r,ms));
    await sleep(80);
    for(let i=0;i<80;i++){
      const pendingMermaid=[...host.querySelectorAll(".mermaid-container")].some(el=>!el.querySelector("svg")&&!el.textContent.trim());
      const pendingCharts=[...host.querySelectorAll("canvas")].some(c=>!c.width||!c.height);
      if(!pendingMermaid&&!pendingCharts)break;
      await sleep(100);
    }
    await sleep(250);
  };

  const exportResearchMarkdown=()=>{
    const report=activeResearch;
    const body=cleanResearchMarkdown(researchLiveMarkdown||report?.report_markdown||"").trim();
    if(!body)return;
    const blob=new Blob([body],{type:"text/markdown"});
    const url=URL.createObjectURL(blob);
    const a=document.createElement("a");
    a.href=url;a.download=researchExportName("md");
    document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  };

  const exportResearchPdf=async()=>{
    const report=activeResearch;
    const body=cleanResearchMarkdown(researchLiveMarkdown||report?.report_markdown||"").trim();
    if(!body){notify({type:"warning",text:"No report body to export"});return;}
    if(!window.html2pdf&&window.ensureHtml2pdf){try{await window.ensureHtml2pdf();}catch{}}
    if(!window.html2pdf){await printResearchReport();return;}
    const host=document.createElement("div");
    host.style.position="fixed";host.style.left="-10000px";host.style.top="0";host.style.width="7.15in";
    host.style.background="#fff";host.style.color="#161b22";host.style.zIndex="-1";
    document.body.appendChild(host);
    const root=ReactDOM.createRoot(host);
    try{
      root.render(<>
        <style>{researchPrintCss()}</style>
        <ResearchPrintPage report={report} markdown={body}/>
      </>);
      await waitForResearchRender(host);
      await window.html2pdf().set({
        margin:[0.58,0.58,0.58,0.58],
        filename:researchExportName("pdf"),
        image:{type:"png",quality:1},
        html2canvas:{scale:2,backgroundColor:"#ffffff",useCORS:true,logging:false},
        jsPDF:{unit:"in",format:"letter",orientation:"portrait"},
        pagebreak:{mode:["css","legacy"],avoid:["pre","blockquote",".chart-print-block",".mermaid-print-block",".sources li","h1","h2","h3","h4"]}
      }).from(host.querySelector(".research-print-page")).save();
    }catch(e){
      notify({type:"error",text:"PDF export failed",detail:e.message||String(e)});
    }finally{
      try{root.unmount();}catch{}
      host.remove();
      try{initMermaidTheme(t,font);setMermaidEpoch(e=>e+1);}catch{}
    }
  };

  const printResearchReport=async()=>{
    const report=activeResearch;
    const body=cleanResearchMarkdown(researchLiveMarkdown||report?.report_markdown||"").trim();
    if(!body){notify({type:"warning",text:"No report body to print"});return;}
    const title=(report?.title||"HyprChat Research Report").replace(/[<>&]/g,c=>({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));
    const w=window.open("","_blank");
    if(!w){notify({type:"error",text:"Print window blocked",detail:"Allow popups for HyprChat, then try Print again."});return;}
    const host=document.createElement("div");
    host.style.position="fixed";host.style.left="-10000px";host.style.top="0";host.style.width="7.15in";
    host.style.background="#fff";host.style.color="#161b22";host.style.zIndex="-1";
    document.body.appendChild(host);
    const root=ReactDOM.createRoot(host);
    try{
      root.render(<>
        <style>{researchPrintCss()}</style>
        <ResearchPrintPage report={report} markdown={body}/>
      </>);
      await waitForResearchRender(host);
      const page=host.querySelector(".research-print-page").cloneNode(true);
      const sourceCanvases=[...host.querySelectorAll("canvas")];
      const cloneCanvases=[...page.querySelectorAll("canvas")];
      sourceCanvases.forEach((canvas,i)=>{
        try{
          const img=document.createElement("img");
          img.src=canvas.toDataURL("image/png");
          img.alt="Chart";
          img.style.maxWidth="100%";
          img.style.display="block";
          cloneCanvases[i]?.replaceWith(img);
        }catch{}
      });
      w.document.open();
      w.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>${title}</title><style>
        ${researchPrintCss()}
      </style></head><body><main>${page.outerHTML}</main><script>setTimeout(()=>{window.focus();window.print();},250);<\/script></body></html>`);
      w.document.close();
    }catch(e){
      w.document.open();
      w.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>${title}</title><style>
        ${researchPrintCss()}
      </style></head><body><main>${buildResearchPrintHtml(report,body)}</main><script>setTimeout(()=>{window.focus();window.print();},250);<\/script></body></html>`);
      w.document.close();
    }finally{
      try{root.unmount();}catch{}
      host.remove();
      try{initMermaidTheme(t,font);setMermaidEpoch(e=>e+1);}catch{}
    }
  };

  // Load conversation messages from backend when switching chats
  const loadConversation = useCallback(async (id, {force=false}={}) => {
    // If clicking the same conversation that already has messages, just switch panel
    if(id===actIdRef.current&&!force){
      // Check if messages exist — if not, fall through to reload from DB
      let _hasMessages=false;
      setConvs(prev=>{_hasMessages=prev.some(c=>c.id===id&&c.messages?.length>0);return prev;});
      if(_hasMessages){setGhostMode(false);setPanel("chat");return;}
    }
    // Save current council stream state to ref before switching away
    if(councilStreamRef.current?.cid===actIdRef.current&&councilStreamRef.current?.running){
      // Already tracked in ref by the stream loop — no action needed
    }
    const reqId = ++loadReqRef.current;
    setConvs(prev=>prev.filter(c=>!isGhostConv(c)));
    setGhostMode(false);
    setActId(id);setPanel("chat");setEvts([]);setExpandedPill(null);setQuickResults([]);
    setVisibleMessageCounts(p=>({...p,[id]:CHAT_HISTORY_RENDER_CHUNK}));
    // Check if we're navigating back to an active council stream
    const activeStream=councilStreamRef.current;
    if(activeStream?.cid===id&&activeStream.running){
      // Restore live council state from the ref
      setCouncilRunning(true);
      setCouncilResponses(activeStream.responses||{});
      setCouncilHostContent(activeStream.hostContent||"");
      setCouncilVotes(activeStream.votes||[]);
      setCouncilVoting(activeStream.voting||false);
      setCouncilRound(activeStream.round||null);
    } else {
      setCouncilRunning(false);setCouncilResponses({});setCouncilHostContent("");setCouncilVotes([]);setCouncilVoting(false);
    }
    setCtxTokens(0);setSessionTokens(0);setTokC(0);setTokS(0);setGenTokens(0);
    setLoadingConv(true);
    try {
      const r = await fetch(`${API}/api/conversations/${id}`);
      if(!r.ok) throw new Error(`HTTP ${r.status}`);
      const full = await r.json();
      if(reqId!==loadReqRef.current) return; // stale — newer request in flight
      if (full && full.messages) {
        const parseMeta=m=>{if(typeof m.metadata==="string"){try{return JSON.parse(m.metadata);}catch{return{};}}return m.metadata||{};};
        const isCouncil=v=>v==="1"||v===1||v===true;
        // If this conversation is being streamed right now, local state is authoritative for messages —
        // skip the overwrite so the stream's m[m.length-1] assistant update doesn't clobber the user message.
        const skipMessages = streamingCidRef.current === id;
        setConvs(prev=>prev.map(c=>c.id===id ? {...c, ...(skipMessages ? {} : {messages: full.messages.map(m=>({id:m.id,role:m.role,content:m.content,metadata:parseMeta(m),created_at:m.created_at}))}), model: full.model||c.model, system_prompt: full.system_prompt||c.system_prompt, tool_ids: full.tool_ids||c.tool_ids||[], model_config_id: full.model_config_id||null, use_memories: full.use_memories??c.use_memories??"0", is_council: isCouncil(full.is_council), council_config_id: full.council_config_id||null} : c));
        // Fetch suggestions for council chats with no messages yet
        if(isCouncil(full.is_council)&&full.council_config_id&&!(full.messages||[]).length){
          fetchCouncilSuggestions(full.council_config_id);
        }
      }
      // Phase 0.6: if a run is still in flight on this conversation (e.g. user closed
      // the tab mid-stream), restore the streaming UI state so the stop button shows
      // and the user knows the agent is still working in the background. Skip when
      // we're already actively streaming this conversation locally.
      if(streamingCidRef.current!==id){
        try{
          const rr = await fetch(`${API}/api/runs?conversation_id=${id}`);
          if(rr.ok){
            const runs = await rr.json();
            const anyRunning = (runs||[]).some(r=>r.status==="running"||r.status==="queued");
            if(anyRunning && reqId===loadReqRef.current){
              // No local fetch to abort — set streaming=true so the input bar
              // shows the stop button. Clicking stop will clear this UI state;
              // the agent itself continues running on the backend until done.
              setStreaming(true);
              streamingCidRef.current = id;
            }
          }
        }catch{}
      }
    } catch(e) { console.error("Failed to load conversation:", e); notify({type:"error",text:"Conversation failed to load",detail:e.message||String(e)}); }
    if(reqId===loadReqRef.current) setLoadingConv(false);
  }, [notify]);

  useEffect(()=>{
    const el=chatScrollRef.current;
    if(!el)return;
    // Only auto-scroll if user is already near the bottom (within 200px)
    const nearBottom=el.scrollHeight-el.scrollTop-el.clientHeight<200;
    if(nearBottom)el.scrollTop=el.scrollHeight;
  },[act?.messages]);
  // Auto-scroll during council streaming
  useEffect(()=>{
    if(!councilRunning)return;
    const el=chatScrollRef.current;
    if(!el)return;
    const nearBottom=el.scrollHeight-el.scrollTop-el.clientHeight<300;
    if(nearBottom)el.scrollTop=el.scrollHeight;
  },[councilResponses,councilHostContent,councilVotes,councilVoting]);
  useEffect(()=>{if(panel==="chat"&&actId){try{inpRef.current?.focus({preventScroll:true});}catch{inpRef.current?.focus();}}if(panel==="models")refreshModels();},[actId,panel]);
  useEffect(()=>{
    if(!downloadsExpanded)return;
    const handler=(e)=>{if(dlPanelRef.current&&!dlPanelRef.current.contains(e.target))setDownloadsExpanded(false);};
    document.addEventListener("mousedown",handler);
    return()=>document.removeEventListener("mousedown",handler);
  },[downloadsExpanded]);
  useEffect(()=>{
    if(!downloads.some(d=>!_activityIsTerminal(d.status)))return;
    const id=setInterval(()=>setActivityNow(Date.now()),1000);
    return()=>clearInterval(id);
  },[downloads]);
  useEffect(()=>{
    if(!researchRunning)return;
    const id=setInterval(()=>setActivityNow(Date.now()),1000);
    return()=>clearInterval(id);
  },[researchRunning]);
  useEffect(()=>{
    if(!currentRunNotice)return;
    const id=setTimeout(()=>setCurrentRunNotice(null),5200);
    return()=>clearTimeout(id);
  },[currentRunNotice]);
  useEffect(()=>{
    const handler=(e)=>{
      if(e.key!=="Escape")return;
      setDownloadsExpanded(false);setShowPromptPicker(false);setShowQuickMenu(false);setShowConnectorPicker(false);setShowEffortPicker(false);setShowExportMenu(false);setShowSysPromptPicker(false);setShowNavMore(false);setRegenPopover(null);setQuickSearchError(null);
      setConfirmDialog(d=>{if(d?.resolve)d.resolve(false);return null;});
      if(panel==="chat")setTimeout(()=>{try{inpRef.current?.focus({preventScroll:true});}catch{inpRef.current?.focus();}},0);
    };
    document.addEventListener("keydown",handler);
    return()=>document.removeEventListener("keydown",handler);
  },[panel]);
  useEffect(()=>{
    if(!showPromptPicker)return;
    const handler=(e)=>{if(promptPickerRef.current&&!promptPickerRef.current.contains(e.target))setShowPromptPicker(false);};
    document.addEventListener("mousedown",handler);
    return()=>document.removeEventListener("mousedown",handler);
  },[showPromptPicker]);
  useEffect(()=>{
    if(!showQuickMenu)return;
    const handler=(e)=>{if(quickMenuRef.current&&!quickMenuRef.current.contains(e.target))setShowQuickMenu(false);};
    document.addEventListener("mousedown",handler);
    return()=>document.removeEventListener("mousedown",handler);
  },[showQuickMenu]);
  useEffect(()=>{
    if(!showConnectorPicker)return;
    const handler=(e)=>{if(connectorPickerRef.current&&!connectorPickerRef.current.contains(e.target))setShowConnectorPicker(false);};
    document.addEventListener("mousedown",handler);
    return()=>document.removeEventListener("mousedown",handler);
  },[showConnectorPicker]);
  useEffect(()=>{
    if(!showExportMenu)return;
    const handler=(e)=>{if(exportMenuRef.current&&!exportMenuRef.current.contains(e.target))setShowExportMenu(false);};
    document.addEventListener("mousedown",handler);
    return()=>document.removeEventListener("mousedown",handler);
  },[showExportMenu]);
  useEffect(()=>{
    if(!showEffortPicker)return;
    const handler=(e)=>{if(effortPickerRef.current&&!effortPickerRef.current.contains(e.target))setShowEffortPicker(false);};
    document.addEventListener("mousedown",handler);
    return()=>document.removeEventListener("mousedown",handler);
  },[showEffortPicker]);
  // Fetch council suggestions
  const fetchCouncilSuggestions=(cfgId)=>{
    if(!cfgId)return;
    setCouncilSugLoading(true);setCouncilSuggestions([]);
    fetch(`${API}/api/councils/${cfgId}/suggestions`).then(r=>r.json()).then(d=>{
      setCouncilSuggestions(d.suggestions||[]);setCouncilSugLoading(false);
    }).catch(()=>{setCouncilSugLoading(false);});
  };

  const uConv=useCallback((id,u)=>{
    if(typeof u!=="function"&&u&&Object.prototype.hasOwnProperty.call(u,"model")){
      modelChoiceRef.current.byConv[id]=u.model||"";
    }
    setConvs(p=>p.map(c=>{
      if(c.id!==id)return c;
      const updated=typeof u==="function"?u(c):{...c,...u};
      const patch={};
      for(const k of ["title","model","system_prompt","tool_ids","persona_name","persona_avatar","is_council","council_config_id","model_config_id","use_memories"]){
        if(JSON.stringify(updated[k])!==JSON.stringify(c[k])&&updated[k]!==undefined)patch[k]=updated[k];
      }
      // Remember last-used model for new chats
      if(patch.model){
        modelChoiceRef.current.byConv[id]=patch.model;
        try{localStorage.setItem("hc-last-model",patch.model);}catch{}
      }
      if(Object.keys(patch).length&&!id.startsWith("l-")&&!id.startsWith("ghost-")){
        fetch(`${API}/api/conversations/${id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(patch)}).catch(()=>{});
      }
      return updated;
    }));
  },[]);
  const revealOlderMessages=useCallback((id,totalMessages)=>{
    if(!id)return;
    const el=chatScrollRef.current;
    const beforeHeight=el?.scrollHeight||0;
    setVisibleMessageCounts(p=>({...p,[id]:Math.min(totalMessages,(p[id]||CHAT_HISTORY_RENDER_CHUNK)+CHAT_HISTORY_RENDER_CHUNK)}));
    requestAnimationFrame(()=>{
      const next=chatScrollRef.current;
      if(next&&beforeHeight)next.scrollTop+=next.scrollHeight-beforeHeight;
    });
  },[]);
  const useModelFromManager=(modelName)=>{
    if(!modelName)return;
    const hasActive=!!(actId&&convs.some(c=>c.id===actId));
    if(hasActive){
      uConv(actId,{model:modelName});
      notify({type:"success",text:"Model selected",detail:modelName,duration:2200});
    }else{
      setPendingChatModel(modelName);
      notify({type:"success",text:"Model selected for new chats",detail:modelName,duration:2600});
    }
    setPanel("chat");
  };
  const leaveActiveProfile=()=>{
    clearPendingProfile();
    if(actId){
      const msgs=act?.messages||[];
      const onlyGreeting=msgs.length===1&&msgs[0]?.metadata?.persona_first_message;
      if(onlyGreeting&&msgs[0]?.id)fetch(`${API}/api/conversations/${actId}/messages/${msgs[0].id}`,{method:"DELETE"}).catch(()=>{});
      uConv(actId,{persona_name:null,persona_avatar:null,tool_ids:[],system_prompt:"",model_config_id:null,...(onlyGreeting?{messages:[]}: {})});
    }else{
      setPanel("chat");
    }
  };

  const newChat=async(forceBlank=false,opts={})=>{
    const curConv=convs.find(c=>c.id===actId);

    if(!forceBlank){
      // If current chat is a council chat, create a new council chat with the same council
      const isCouncilChat=curConv&&(curConv.is_council==="1"||curConv.is_council===1||curConv.is_council===true)&&curConv.council_config_id;
      if(isCouncilChat){
        await startCouncilChat(curConv.council_config_id);
        return;
      }
    }

    // Figure out persona to carry over
    let persona=null;
    if(forceBlank&&opts.personaOverride){
      persona={...opts.personaOverride,tool_ids:opts.personaOverride.tool_ids||[]};
    }else if(forceBlank&&opts.carryCurrent&&curConv?.persona_name){
      persona={model:curConv.model,system_prompt:curConv.system_prompt||"",tool_ids:curConv.tool_ids||[],model_config_id:curConv.model_config_id,persona_name:curConv.persona_name,persona_avatar:curConv.persona_avatar};
    }else if(forceBlank&&pendingPersona){
      persona={...pendingPersona,tool_ids:pendingToolIds};
    }else if(!forceBlank){
      if(curConv?.persona_name){
        persona={model:curConv.model,system_prompt:curConv.system_prompt||"",tool_ids:curConv.tool_ids||[],model_config_id:curConv.model_config_id,persona_name:curConv.persona_name,persona_avatar:curConv.persona_avatar};
      }
    }

    const lastModel=storedLastModel();
    const currentModel=curConv?(modelChoiceRef.current.byConv[curConv.id]||curConv.model):"";
    let defaultModel=persona?.model||currentModel||(forceBlank?(modelChoiceRef.current.pending||pendingChatModel):"")||lastModel||models[0]||"qwen3.5:27b";
    if(defaultModel&&models.length>0&&!models.includes(defaultModel))defaultModel=models[0]||"";
    const pendingTools=forceBlank?(persona?.tool_ids||(opts.carryCurrent?(curConv?.tool_ids||[]):pendingToolIds)):[];
    const pendingEffortValue=forceBlank&&pendingEffort!==null?pendingEffort:null;
    const useGhost=ghostMode||opts.ephemeral;
    const pendingMemoryValue=(!useGhost&&forceBlank&&pendingUseMemories)?"1":"0";
    if(useGhost){
      const newId=`ghost-${Date.now()}-${Math.random().toString(16).slice(2,8)}`;
      const c={id:newId,title:"Ghost Chat",messages:[],model:defaultModel,system_prompt:persona?.system_prompt||(opts.carryCurrent?(curConv?.system_prompt||""):""),...(persona||{}),tool_ids:pendingTools,ephemeral:true};
      modelChoiceRef.current.byConv[newId]=c.model||defaultModel;
      const firstMsg=persona?makeProfileFirstMessage(persona):null;
      if(firstMsg)c.messages=[firstMsg];
      if(pendingEffortValue!==null)setEffortPerChat(p=>({...p,[newId]:pendingEffortValue}));
      setPendingUseMemories(false);
      setConvs(p=>[c,...p]);setActId(newId);setGhostMode(true);
      setPanel("chat");setEvts([]);setSessionTokens(0);setCtxTokens(0);setGenTokens(0);setExpandedPill(null);setCouncilRunning(false);setCouncilResponses({});setCouncilHostContent("");
      return opts.returnConv?c:newId;
    }
    try{
      const r=await fetch(`${API}/api/conversations`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({title:"New Chat",model:defaultModel,use_memories:pendingMemoryValue})});
      const c=await r.json();c.messages=[];c.use_memories=c.use_memories??pendingMemoryValue;
      modelChoiceRef.current.byConv[c.id]=c.model||defaultModel;
      // Apply persona directly to the conv object before adding to state
      if(persona){
        Object.assign(c,persona);
        fetch(`${API}/api/conversations/${c.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(persona)}).catch(()=>{});
      }
      const firstMsg=persona?await persistProfileFirstMessage(c.id,persona):null;
      if(firstMsg)c.messages=[firstMsg];
      if(pendingTools.length){
        c.tool_ids=pendingTools;
        fetch(`${API}/api/conversations/${c.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({tool_ids:pendingTools})}).catch(()=>{});
      }
      if(pendingEffortValue!==null)setEffortPerChat(p=>({...p,[c.id]:pendingEffortValue}));
      setPendingUseMemories(false);
      setConvs(p=>[c,...p]);setActId(c.id);
      setPanel("chat");setEvts([]);setSessionTokens(0);setCtxTokens(0);setGenTokens(0);setExpandedPill(null);setCouncilRunning(false);setCouncilResponses({});setCouncilHostContent("");
      return opts.returnConv?c:c.id;
    }catch{
      const newId=`l-${Date.now()}`;
      const c={id:newId,title:"New Chat",messages:[],model:defaultModel,system_prompt:persona?.system_prompt||"",...(persona||{}),tool_ids:pendingTools,use_memories:pendingMemoryValue};
      modelChoiceRef.current.byConv[newId]=c.model||defaultModel;
      const firstMsg=persona?makeProfileFirstMessage(persona):null;
      if(firstMsg)c.messages=[firstMsg];
      if(pendingEffortValue!==null)setEffortPerChat(p=>({...p,[newId]:pendingEffortValue}));
      setPendingUseMemories(false);
      setConvs(p=>[c,...p]);setActId(newId);
      setPanel("chat");setEvts([]);setSessionTokens(0);setCtxTokens(0);setGenTokens(0);setExpandedPill(null);setCouncilRunning(false);setCouncilResponses({});setCouncilHostContent("");
      return opts.returnConv?c:newId;
    }
  };

  const startBlankChat=()=>{
    setGhostMode(false);
    setPendingUseMemories(false);
    setConvs(p=>p.filter(c=>!isGhostConv(c)));
    setActId(null);
    clearPendingProfile();
    setPanel("chat");
    setInp("");
    setAttachments([]);
    setEvts([]);
    setQuickResults([]);
    setSessionTokens(0);
    setCtxTokens(0);
    setGenTokens(0);
    setExpandedPill(null);
    setCouncilRunning(false);
    setCouncilResponses({});
    setCouncilHostContent("");
    setLoadingConv(false);
  };

  const toggleGhostMode=async()=>{
    if(streaming||councilRunning){
      notify({type:"warning",text:"Finish the current response first",detail:"Ghost mode can only change between turns."});
      return;
    }
    if(ghostActive){
      setGhostMode(false);
      if(isGhostConv(act)){
        setConvs(p=>p.filter(c=>!isGhostConv(c)));
        setActId(null);
        setEvts([]);setQuickResults([]);setSessionTokens(0);setCtxTokens(0);setGenTokens(0);setExpandedPill(null);
      }
      notify({type:"info",text:"Ghost mode off",detail:"Saved chats will work normally again.",duration:2600});
      return;
    }
    setGhostMode(true);
    if(act&&!isGhostConv(act)){
      await newChat(true,{ephemeral:true,carryCurrent:true});
      notify({type:"info",text:"Ghost chat started",detail:"This chat stays local and is discarded on reload.",duration:3600});
    }else{
      notify({type:"info",text:"Ghost mode on",detail:"The next chat will not be saved or indexed.",duration:3600});
    }
  };

  const closeSidebarSearch=()=>{
    setShowMessageSearch(false);
    setSq("");
    setFtsQuery("");
    setFtsResults([]);
  };
  const openSidebarSearch=(focus="titles")=>{
    setSidebar(true);
    setWsPanel(false);
    setShowMessageSearch(true);
    setTimeout(()=>{(focus==="messages"?ftsRef:titleSearchRef).current?.focus();},80);
  };

  // ── Keyboard Shortcuts ──
  useEffect(()=>{
    const handler=(e)=>{
      if((e.ctrlKey||e.metaKey)&&e.key==="k"){e.preventDefault();openSidebarSearch("messages");}
      if((e.ctrlKey||e.metaKey)&&e.key==="n"){e.preventDefault();startBlankChat();}
      if((e.ctrlKey||e.metaKey)&&e.key==="/"){e.preventDefault();setSidebar(p=>!p);}
      if(e.key==="Escape"){if(showPromptPicker)setShowPromptPicker(false);else if(previewFile){setPreviewFile(null);setPreviewContent(null);setPreviewError(null);setPreviewArchive(null);}else if(showSysPromptPicker)setShowSysPromptPicker(false);else if(ftsQuery||sq){setSq("");setFtsQuery("");setFtsResults([]);}else if(showMessageSearch)closeSidebarSearch();}
    };
    document.addEventListener("keydown",handler);
    return ()=>document.removeEventListener("keydown",handler);
  });

  const delChat=async id=>{
    if(!String(id||"").startsWith("ghost-"))fetch(`${API}/api/conversations/${id}`,{method:"DELETE"}).catch(()=>{});
    setConvTags(p=>{const n={...p};delete n[id];return n;});
    setConvs(p=>{const n=p.filter(c=>c.id!==id);if(actId===id){setActId(n[0]?.id||null);setCouncilRunning(false);setCouncilResponses({});setCouncilHostContent("");}return n;});
  };

  // Full-text search across conversations
  const doFtsSearch=useCallback(async(q)=>{
    if(!q.trim()){setFtsResults([]);return;}
    setFtsLoading(true);
    try{
      const r=await fetch(`${API}/api/conversations/search`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({query:q,limit:20})});
      const data=await r.json();
      setFtsResults(Array.isArray(data)?data:[]);
    }catch(e){console.error("FTS search error",e);setFtsResults([]);notify({type:"warning",text:"Search failed",detail:e.message||String(e),duration:5000});}
    finally{setFtsLoading(false);}
  },[notify]);
  useEffect(()=>{const t=setTimeout(()=>{if(ftsQuery.length>=2)doFtsSearch(ftsQuery);else setFtsResults([])},300);return()=>clearTimeout(t);},[ftsQuery,doFtsSearch]);

  // Fork conversation at a specific message
  const forkAt=async(msgIndex)=>{
    if(!actId)return;
    try{
      const full=await(await fetch(`${API}/api/conversations/${actId}`)).json();
      const msgs=full.messages||[];
      if(msgIndex>=msgs.length)return;
      const dbMsg=msgs[msgIndex];
      if(!dbMsg?.id)return;
      const r=await fetch(`${API}/api/conversations/${actId}/fork`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message_id:dbMsg.id})});
      if(!r.ok)throw new Error(`HTTP ${r.status}`);
      const forked=await r.json();
      setConvs(p=>[{id:forked.id,title:forked.title,model:forked.model,forked_from:actId,updated_at:new Date().toISOString()},...p]);
      loadConversation(forked.id,{force:true});
    }catch(e){console.error("Fork error",e);notify({type:"error",text:"Fork failed",detail:e.message||String(e)});}
  };

  // Load analytics data
  const loadAnalytics=useCallback(async()=>{
    try{
      const [summary,grouped]=await Promise.all([
        fetch(`${API}/api/analytics/tokens/summary`).then(r=>r.json()),
        fetch(`${API}/api/analytics/tokens?days=${analyticsDays}&group_by=${analyticsGroup}`).then(r=>r.json())
      ]);
      setAnalyticsData({summary,grouped:Array.isArray(grouped)?grouped:[]});
    }catch(e){console.error("Analytics error",e);notify({type:"warning",text:"Analytics failed to load",detail:e.message||String(e),duration:5000});}
  },[analyticsDays,analyticsGroup,notify]);
  useEffect(()=>{if(panel==="analytics")loadAnalytics();},[panel,loadAnalytics]);

  const clearDeletedModelRefs=(deletedModels=[],newModels=models)=>{
    const deletedSet=new Set((deletedModels||[]).filter(Boolean));
    if(!deletedSet.size)return;
    const fallback=(newModels||[]).find(m=>!deletedSet.has(m))||"";
    setConvs(prev=>prev.map(c=>{
      if(deletedSet.has(c.model)){
        const patch={model:fallback};
        if(!c.id.startsWith("l-")&&!c.id.startsWith("ghost-"))fetch(`${API}/api/conversations/${c.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(patch)}).catch(()=>{});
        return {...c,...patch};
      }
      return c;
    }));
    if(deletedSet.has(planningModel))setPlanningModel("");
    if(deletedSet.has(coderModel))setCoderModel("");
    if(deletedSet.has(architectModel))setArchitectModel("");
    if(deletedSet.has(reviewerModel))setReviewerModel("");
    if(deletedSet.has(acceptanceModel))setAcceptanceModel("");
    if(deletedSet.has(builderModel))setBuilderModel("");
    if(deletedSet.has(fixerModel))setFixerModel("");
    if(deletedSet.has(qaModel))setQaModel("");
    if(deletedSet.has(wsModel))setWsModel(fallback);
    if(deletedSet.has(modelParamsOpen))setModelParamsOpen(null);
    try{const last=localStorage.getItem("hc-last-model")||"";if(deletedSet.has(last))localStorage.setItem("hc-last-model",fallback);}catch{}
  };
  const refreshModelListAfterDelete=async(deletedModels=[])=>{
    let next=[];
    try{
      const r=await fetch(`${API}/api/models`);
      const d=await r.json().catch(()=>({}));
      if(r.ok){next=d.models||[];setModels(next);setModelDetails(d.model_details||{});}
    }catch{}
    clearDeletedModelRefs(deletedModels,next);
    return next;
  };
  const clearAllChats=async()=>{
    const ok=await confirmAction({title:"Delete all chats",body:"Delete all conversations? This cannot be undone.",confirmLabel:"Delete All Chats",tone:"danger"});
    if(!ok)return;
    try{
      const r=await fetch(`${API}/api/conversations`,{method:"DELETE"});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      setConvs([]);setVisibleMessageCounts({});setActId(null);setEvts([]);evtsRef.current=[];streamSaveEvtsRef.current=[];setCoderWorkflows([]);
      notify({type:"success",text:"Chats deleted",detail:`Deleted ${d.deleted||0} conversations.`});
    }catch(e){notify({type:"error",text:"Delete failed",detail:e.message});}
  };
  const clearAllMemories=async()=>{
    const ok=await confirmAction({title:"Clear all memories",body:"Delete all global and workspace memories for the current user? Chats, artifacts, models, and statistics are kept.",confirmLabel:"Clear Memories",tone:"danger"});
    if(!ok)return;
    try{
      const r=await fetch(`${API}/api/memory/memories`,{method:"DELETE"});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      notify({type:"success",text:"Memories cleared",detail:`Deleted ${d.deleted||0} memories.`});
    }catch(e){notify({type:"error",text:"Memory clear failed",detail:e.message});}
  };
  const clearAllArtifacts=async()=>{
    const ok=await confirmAction({title:"Clear all artifacts",body:"Delete every artifact record and removable artifact file for the current user? Chats, memories, models, and statistics are kept.",confirmLabel:"Clear Artifacts",tone:"danger"});
    if(!ok)return;
    try{
      const r=await fetch(`${API}/api/artifacts`,{method:"DELETE"});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      setArtifactFocusId(null);
      notify({type:"success",text:"Artifacts cleared",detail:`Deleted ${d.deleted||0} artifacts and ${d.deleted_files||0} file(s).`});
    }catch(e){notify({type:"error",text:"Artifact clear failed",detail:e.message});}
  };
  const clearStatistics=async()=>{
    const ok=await confirmAction({title:"Clear statistics",body:"Delete all token usage and analytics rows for the current user? This does not affect chats or models.",confirmLabel:"Clear Statistics",tone:"danger"});
    if(!ok)return;
    try{
      const r=await fetch(`${API}/api/analytics/tokens`,{method:"DELETE"});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      setAnalyticsData(null);
      if(panel==="analytics")loadAnalytics();
      notify({type:"success",text:"Statistics cleared",detail:`Deleted ${d.deleted||0} usage rows.`});
    }catch(e){notify({type:"error",text:"Statistics clear failed",detail:e.message});}
  };
  const deleteAllModels=async()=>{
    const ok=await confirmAction({title:"Delete all models",body:"Delete every locally installed Ollama model? Cloud provider keys and HuggingFace search data are not deleted. Type DELETE MODELS to confirm.",confirmLabel:"Delete All Models",tone:"danger",requiredText:"DELETE MODELS"});
    if(!ok)return;
    try{
      const r=await fetch(`${API}/api/models`,{method:"DELETE"});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      await refreshModelListAfterDelete(d.models||[]);
      notify({type:d.failed?.length?"warning":"success",text:d.failed?.length?"Models partially deleted":"Models deleted",detail:`Deleted ${d.deleted||0} model(s)${d.failed?.length?`; ${d.failed.length} failed`:""}.`,duration:6000});
    }catch(e){notify({type:"error",text:"Model delete failed",detail:e.message});}
  };
  const deleteOtherUsers=async()=>{
    const count=Math.max(0,(users||[]).filter(u=>u.id!==currentUserId).length);
    const ok=await confirmAction({title:"Delete other users",body:`Delete ${count} other user profile${count===1?"":"s"} and all data associated with them? The current user (${currentUser?.name||currentUserId||"current"}) is kept. Type DELETE OTHER USERS to confirm.`,confirmLabel:"Delete Other Users",tone:"danger",requiredText:"DELETE OTHER USERS"});
    if(!ok)return;
    try{
      const r=await fetch(`${API}/api/users`,{method:"DELETE"});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      try{(users||[]).forEach(u=>{if(u.id!==currentUserId)localStorage.removeItem(hcSessionKey(u.id));});}catch{}
      await refreshUsers().catch(()=>{});
      notify({type:"success",text:"Other users deleted",detail:`Deleted ${d.deleted||0} profile(s).`});
    }catch(e){notify({type:"error",text:"User cleanup failed",detail:e.message});}
  };
  const freshInstallReset=async()=>{
    const ok=await confirmAction({title:"Delete all / fresh install",body:"Delete all users, sessions, chats, memories, artifacts, statistics, and locally installed Ollama models? This includes the current user. Type DELETE EVERYTHING to confirm.",confirmLabel:"Delete Everything",tone:"danger",requiredText:"DELETE EVERYTHING"});
    if(!ok)return;
    try{
      const r=await fetch(`${API}/api/danger-zone/fresh-install`,{method:"POST"});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      try{
        Object.keys(localStorage).forEach(k=>{if(k===HC_USER_KEY||k.startsWith(HC_SESSION_PREFIX)||k==="hc-last-model")localStorage.removeItem(k);});
      }catch{}
      hcSetUserContext("default","");
      resetUserScopedState();setUsers([]);setCurrentUser(null);setCurrentUserId("default");setLoginUserId("default");setLoginPassword("");setNewUserName("");setNewUserPassword("");
      setAuthReady(false);setUserGateOpen(true);setModels([]);setModelDetails({});setAnalyticsData(null);setLoginError("Fresh install complete. Create a new profile to continue.");
      notify({type:d.models?.status==="failed"?"warning":"success",text:"Fresh install reset complete",detail:d.models?.status==="failed"?`User data was reset. Model cleanup failed: ${d.models.error}`:`Deleted ${d.users_deleted||0} profile(s) and ${d.models?.deleted||0} model(s).`,duration:8000});
    }catch(e){notify({type:"error",text:"Fresh install reset failed",detail:e.message,duration:9000});}
  };

  // Start a new council chat session
  const startCouncilChat=async(councilId)=>{
    const council=councils.find(c=>c.id===councilId);
    if(!council)return;
    try{
      const r=await fetch(`${API}/api/conversations`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({title:`⚖️ ${council.name}`,model:council.host_model||models[0]||"qwen3.5:27b"})});
      if(!r.ok)throw new Error(`HTTP ${r.status}`);
      const c=await r.json();
      // patch is_council + council_config_id
      await fetch(`${API}/api/conversations/${c.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({is_council:"1",council_config_id:councilId})}).catch(()=>{});
      c.messages=[];c.is_council=true;c.council_config_id=councilId;
      setConvs(p=>[c,...p]);setActId(c.id);setPanel("chat");setEvts([]);setQuickResults([]);
      fetchCouncilSuggestions(councilId);
    }catch(err){console.error("Council chat start failed",err);notify({type:"error",text:"Council chat failed to start",detail:err.message||String(err)});}
  };

  // Send a message to the council
  const sendCouncil=async()=>{
    if(!inp.trim()||councilRunning)return;
    const cid=actId;const cv=convs.find(c=>c.id===cid);
    if(!cv?.council_config_id)return;
    const userMsg=inp.trim();setInp("");setCouncilSuggestions([]);if(inpRef.current){inpRef.current.style.height="auto";}
    // Quick search runs server-side in council.py (one SearXNG hit shared by
    // members + carousel). Its search_results / tool_done events arrive on the
    // conversation EventBus, which populates quickResults and clears loading.
    if(quickSearch&&userMsg){
      setSearchLoading(true);setQuickResults([]);setQuickSearchError(null);
    }else{setQuickResults([]);setQuickSearchError(null);}
    // Add user message to UI (backend council.py also saves it, so don't POST here)
    uConv(cid,c=>({...c,messages:[...(c.messages||[]),{role:"user",content:userMsg,metadata:{},created_at:new Date().toISOString()}]}));
    // Title generation
    if(!(cv.messages||[]).length){const title=userMsg.slice(0,40)+(userMsg.length>40?"...":"");uConv(cid,{title});fetch(`${API}/api/conversations/${cid}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({title})}).catch(()=>{});}
    // Start council streaming
    setCouncilRunning(true);setCouncilResponses({});setCouncilHostContent("");setCouncilVotes([]);setCouncilVoting(false);setCouncilRound(null);setCouncilKbStatus("");
    // Initialize stream ref for cross-navigation persistence
    const sRef={cid,running:true,responses:{},hostContent:"",votes:[],voting:false,round:null};
    councilStreamRef.current=sRef;
    const msgs=(cv.messages||[]).map(m=>({role:m.role,content:m.content}));
    msgs.push({role:"user",content:userMsg});
    try{
      const res=await fetch(`${API}/api/council/chat/stream`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({conversation_id:cid,council_id:cv.council_config_id,messages:msgs,quick_search:quickSearch})});
      const rdr=res.body.getReader(),dec=new TextDecoder();let buf="";
      while(true){
        const{done,value}=await rdr.read();if(done)break;
        buf+=dec.decode(value,{stream:true});
        const lines=buf.split("\n");buf=lines.pop()||"";
        for(const ln of lines){
          if(!ln.startsWith("data: "))continue;
          try{const d=JSON.parse(ln.slice(6));
            if(d.type==="council_kb"){
              setCouncilKbStatus(d.status||"");
            }else if(d.type==="council_round"){
              const rnd={round:d.round,total_rounds:d.total_rounds,label:d.label};
              setCouncilRound(rnd);sRef.round=rnd;
              // On new debate round, reload messages from backend to get previous rounds persisted
              if(d.round>0){
                setCouncilResponses({});sRef.responses={};
                fetch(`${API}/api/conversations/${cid}`).then(r=>r.ok?r.json():null).then(full=>{
                  if(full?.messages){
                    const parseMeta=m=>{if(typeof m.metadata==="string"){try{return JSON.parse(m.metadata);}catch{return{};}}return m.metadata||{};};
                    setConvs(prev=>prev.map(c=>c.id===cid?{...c,messages:full.messages.map(m=>({role:m.role,content:m.content,metadata:parseMeta(m)}))}:c));
                  }
                }).catch(()=>{});
              }
            }else if(d.type==="council_token"){
              setCouncilResponses(p=>{const n={...p,[d.member_id]:{model:d.model,member_name:d.member_name,content:(p[d.member_id]?.content||"")+d.content,done:false,round:d.round,round_label:d.round_label,responding_to:d.responding_to||[]}};sRef.responses=n;return n;});
            }else if(d.type==="council_done"){
              setCouncilResponses(p=>{const n={...p,[d.member_id]:{...(p[d.member_id]||{model:d.model,content:""}),model:d.model,member_name:d.member_name,done:true,round:d.round,round_label:d.round_label,responding_to:d.responding_to||p[d.member_id]?.responding_to||[]}};sRef.responses=n;return n;});
            }else if(d.type==="council_voting"){
              setCouncilVoting(true);sRef.voting=true;
              setCouncilResponses({});sRef.responses={};
              fetch(`${API}/api/conversations/${cid}`).then(r=>r.ok?r.json():null).then(full=>{
                if(full?.messages){
                  const parseMeta=m=>{if(typeof m.metadata==="string"){try{return JSON.parse(m.metadata);}catch{return{};}}return m.metadata||{};};
                  setConvs(prev=>prev.map(c=>c.id===cid?{...c,messages:full.messages.map(m=>({role:m.role,content:m.content,metadata:parseMeta(m)}))}:c));
                }
              }).catch(()=>{});
            }else if(d.type==="council_vote"){
              setCouncilVotes(p=>{const n=[...p,{voter_id:d.voter_id,voter_name:d.voter_name,voted_for:d.voted_for,voted_for_name:d.voted_for_name,reason:d.reason}];sRef.votes=n;return n;});
            }else if(d.type==="council_votes"){
              setCouncilVoting(false);sRef.voting=false;
              if(d.updated_points){setCouncils(p=>p.map(c=>c.id===cv.council_config_id?{...c,members:(c.members||[]).map(m=>d.updated_points[m.id]!==undefined?{...m,points:d.updated_points[m.id]}:m)}:c));}
            }else if(d.type==="council_host_token"){
              setCouncilHostContent(p=>{const n=p+d.content;sRef.hostContent=n;return n;});
            }else if(d.type==="council_complete"){
              sRef.running=false;
              setCouncilResponses({});setCouncilHostContent("");setCouncilVotes([]);setCouncilVoting(false);setCouncilKbStatus("");
              // Reload conversation from backend to get all rounds with correct metadata
              try{
                const fullR=await fetch(`${API}/api/conversations/${cid}`);
                if(fullR.ok){
                  const full=await fullR.json();
                  if(full&&full.messages){
                    const parseMeta=m=>{if(typeof m.metadata==="string"){try{return JSON.parse(m.metadata);}catch{return{};}}return m.metadata||{};};
                    setConvs(prev=>prev.map(c=>c.id===cid?{...c,messages:full.messages.map(m=>({role:m.role,content:m.content,metadata:parseMeta(m)}))}:c));
                  }
                }
              }catch(e){console.error("Failed to reload council conversation",e);notify({type:"warning",text:"Council history reload failed",detail:e.message||String(e),duration:5000});}
            }
          }catch{}
        }
      }
    }catch(e){console.error("Council stream error",e);notify({type:"error",text:"Council stream failed",detail:e.message||String(e)});}
    sRef.running=false;
    if(councilStreamRef.current===sRef)councilStreamRef.current=null;
    setCouncilRunning(false);
  };

  // Update council member score
  const scoreCouncilMember=async(councilId,memberId,delta)=>{
    const council=councils.find(c=>c.id===councilId);if(!council)return;
    const member=(council.members||[]).find(m=>m.id===memberId);if(!member)return;
    const newPoints=(member.points||0)+delta;
    await fetch(`${API}/api/councils/members/${memberId}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({points:newPoints})}).catch(()=>{});
    setCouncils(p=>p.map(c=>c.id===councilId?{...c,members:c.members.map(m=>m.id===memberId?{...m,points:newPoints}:m)}:c));
  };

  // Rough token estimate: ~4 chars per token
  const estimateTokens = (msgs) => msgs.reduce((sum,m)=>sum+Math.ceil((m.content||"").length/4),0);

  // Core send logic — used by send, regenerate, and edit
  const sendMessages=async(cid, messagesToSend, appendUser, overrides)=>{
    overrides = overrides || {};
    setStreaming(true);setTokC(0);setTokS(0);setEvts([]);setExpandedPill(null);setCtxTokens(0);setGenTokens(0);
    setCurrentRunNotice(null);
    streamSaveEvtsRef.current=[];
    streamingCidRef.current=cid;
    // User message persistence is handled server-side by chat_stream_generate's defensive save —
    // doing it from the client too would race and create duplicates.
    try{
      const stateConv=convs.find(c=>c.id===cid);
      const cv=overrides.conversation||stateConv||{tool_ids:pendingToolIds,system_prompt:pendingPersona?.system_prompt||"",model_config_id:pendingPersona?.model_config_id||null,persona_name:pendingPersona?.persona_name||null,persona_avatar:pendingPersona?.persona_avatar||null,model:pendingPersona?.model,use_memories:overrides.freshChat&&pendingUseMemories?"1":"0"};
      const isGhostSend=!!overrides.ephemeral||ghostMode||isGhostConv(cv)||String(cid||"").startsWith("ghost-");
      let am = messagesToSend.map(m=>{
        let baseContent=m._fullContent||m.content||"";
        // After reload _fullContent is gone; reconstruct the `[Attached N image: ...]` hint
        // from persisted metadata so non-vision models still know an image was attached.
        const metaImgs=(m.metadata&&Array.isArray(m.metadata.images))?m.metadata.images:null;
        if(!m._fullContent&&metaImgs&&metaImgs.length){
          const names=metaImgs.map(im=>im.name||"image").join(", ");
          const hint=`[Attached ${metaImgs.length} image${metaImgs.length>1?"s":""}: ${names}]`;
          baseContent=baseContent?`${baseContent}\n\n${hint}`:hint;
        }
        const out={role:m.role,content:baseContent};
        // Pass images from current send (_images) or persisted user metadata (metadata.images)
        const persistedImgs=metaImgs?metaImgs.map(im=>(im.dataUrl||"").split(",")[1]||"").filter(Boolean):null;
        const imgs=(Array.isArray(m._images)&&m._images.length)?m._images:(persistedImgs&&persistedImgs.length?persistedImgs:null);
        if(imgs)out.images=imgs;
        return out;
      });
      // Get model-specific params, falling back to globals
      let modelName=overrides.model||modelChoiceRef.current.byConv[cid]||cv?.model||models[0]||"";
      // If the selected model is no longer available, fall back to first available
      if(modelName&&models.length>0&&!models.includes(modelName)){
        const fb=models[0]||"";
        console.warn(`Model '${modelName}' no longer available, falling back to '${fb}'`);
        modelName=fb;
        if(fb)uConv(cid,{model:fb});
      }
      // Remember last-used model for new chats
      if(modelName)try{localStorage.setItem("hc-last-model",modelName);}catch{}
      const mp=modelParams[modelName]||{};
      const resolveParam=(mpVal,globalVal)=>{
        if(mpVal===undefined||mpVal===""||mpVal==="default")return globalVal||undefined;
        const n=parseFloat(mpVal);return isNaN(n)?undefined:n;
      };
      const sendTemp=overrides.temperature!==undefined?overrides.temperature:resolveParam(mp.temperature,defaultTemp);
      const sendTopP=resolveParam(mp.top_p,defaultTopP);
      const sendNumCtx=resolveParam(mp.num_ctx,numCtx)||undefined;
      const sendTopK=mp.top_k&&mp.top_k!=="default"?parseInt(mp.top_k)||undefined:undefined;
      const sendRepeatPenalty=mp.repeat_penalty&&mp.repeat_penalty!=="default"?parseFloat(mp.repeat_penalty)||undefined:undefined;

      // Context window limiting — trim oldest messages to fit within num_ctx
      const effectiveLimit = sendNumCtx || tokenLimit;
      if(effectiveLimit>0){
        let est=estimateTokens(am);
        while(est>effectiveLimit && am.length>2){
          am.splice(0,1);
          est=estimateTokens(am);
        }
      }

      const ctrl=new AbortController();abortR.current=ctrl;
      const body={conversation_id:cid,model:modelName,messages:am,system_prompt:cv?.system_prompt||"",tool_ids:cv?.tool_ids||[],persona_id:overrides.persona_id!==undefined?overrides.persona_id:(cv?.model_config_id||null),use_memories:!isGhostSend&&(cv?.use_memories==="1"||cv?.use_memories===1||cv?.use_memories===true)};
      if(isGhostSend)body.ephemeral=true;
      const _wsForChat=(wsDetail&&wsDetail.id&&Array.isArray(wsDetail.conversations)&&wsDetail.conversations.some(wc=>wc.id===cid))?wsDetail:null;
      if(_wsForChat?.id&&!isGhostSend)body.workspace_id=_wsForChat.id;
      // Send the user's typed text + attachment metadata separately so the backend's
      // defensive save persists the clean content + image previews (not the bloated
      // `[Attached N image: ...]` model-facing string).
      const _lastUser=[...messagesToSend].reverse().find(mm=>mm.role==="user");
      if(_lastUser){
        const _md=_lastUser.metadata||null;
        const _cleanMeta=(_md&&((_md.images&&_md.images.length)||(_md.pdfs&&_md.pdfs.length)))?{
          ...(_md.images?{images:_md.images}:{}),
          ...(_md.pdfs?{pdfs:_md.pdfs}:{}),
        }:null;
        if(typeof _lastUser.content==="string")body.display_content=_lastUser.content;
        if(_cleanMeta)body.user_metadata=_cleanMeta;
      }
      if(sendNumCtx)body.num_ctx=sendNumCtx;
      if(sendTemp!==undefined)body.temperature=sendTemp;
      if(sendTopP!==undefined)body.top_p=sendTopP;
      if(sendTopK!==undefined)body.top_k=sendTopK;
      if(sendRepeatPenalty!==undefined)body.repeat_penalty=sendRepeatPenalty;
      const _activeProfile=body.persona_id?mcs.find(mc=>mc.id===body.persona_id):null;
      const _activeProfileParams=_activeProfile?normalizeProfileParams(_activeProfile):null;
      const _personaThinkMode=_activeProfileParams?.profile_type==="persona"?normalizePersonaThinkingMode(_activeProfileParams.persona?.thinking_mode):"auto";
      if(_personaThinkMode!=="auto")body.think_budget=_personaThinkMode==="off"?0:1;
      else if(thinkMode!=="auto")body.think_budget=thinkMode==="off"?0:1;
      const _effort=resolveEffort(cid,overrides);
      if(_effort>0)body.effort_rounds=_effort;
      const res=await fetch(`${API}/api/chat/stream`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body),signal:ctrl.signal});
      if(!res.ok){const errText=await res.text().catch(()=>res.statusText);throw new Error(`HTTP ${res.status}: ${errText}`);}
      const rdr=res.body.getReader(),dec=new TextDecoder();
      let full="",buf="",refinementsCount=0;
      // Phase 0.6: backend creates the assistant message at stream start and sends
      // its id via an `init` SSE event. We PATCH this row on stream-complete instead
      // of POSTing a duplicate, so disconnect-then-reload still leaves exactly one
      // assistant message per turn.
      let _streamMsgId=null;
      while(true){
        const{done,value}=await rdr.read();if(done)break;
        buf+=dec.decode(value,{stream:true});
        const lines=buf.split("\n");buf=lines.pop()||"";
        for(const ln of lines){
          if(!ln.startsWith("data: "))continue;
          try{const d=JSON.parse(ln.slice(6));
            if(d.type==="init"){_streamMsgId=d.message_id;}
            else if(d.type==="token"){full+=d.content;const shown=replacePersonaPlaceholdersForConversation(full,cv);uConv(cid,c=>{const m=[...(c.messages||[])];m[m.length-1]={...m[m.length-1],role:"assistant",content:shown,isS:true};return{...c,messages:m};});}
            else if(d.type==="clear"){full="";uConv(cid,c=>{const m=[...(c.messages||[])];m[m.length-1]={...m[m.length-1],role:"assistant",content:"",isS:true};return{...c,messages:m};});}
            else if(d.type==="refinement_start"){full="";refinementsCount=d.round||refinementsCount;uConv(cid,c=>{const m=[...(c.messages||[])];m[m.length-1]={...m[m.length-1],role:"assistant",content:"",isS:true};return{...c,messages:m};});const _refEv={type:"tool_start",data:{tool:"refinement",status:`Refining answer (${d.round}/${d.total})...`,icon:"sparkles",round:d.round,total:d.total},timestamp:Date.now()/1000};streamSaveEvtsRef.current.push(_refEv);setEvts(p=>[...p.slice(-200),_refEv]);}
            else if(d.type==="done"){if(d.message_id)_streamMsgId=d.message_id;setTokS(d.speed);if(d.gen_tokens||d.tokens)setSessionTokens(p=>p+((d.gen_tokens||d.tokens)||0));if(d.prompt_tokens)setCtxTokens(d.prompt_tokens+(d.gen_tokens||0));if(d.gen_tokens){setGenTokens(d.gen_tokens);setTokC(d.gen_tokens);}if(d.refinements)refinementsCount=d.refinements;}
            else if(d.type==="ctx_update"){if(d.gen_tokens)setTokC(d.gen_tokens);if(d.prompt_tokens)setCtxTokens(d.prompt_tokens);}
            else if(d.type==="error"){const _errBlock=(full?"\n\n":"")+"```\n⚠ "+d.error+"\n```";full=full+_errBlock;const shown=replacePersonaPlaceholdersForConversation(full,cv);uConv(cid,c=>{const m=[...(c.messages||[])];m[m.length-1]={...m[m.length-1],role:"assistant",content:shown,isS:false};return{...c,messages:m};});}
          }catch{}}
      }
      // Capture relevant events into metadata for persistence (tool status + search results + source links)
      // Prefer the persistent stream-save buffer, which survives chat switches. Fall back to evtsRef for safety.
      const _rawEvts = streamSaveEvtsRef.current.length > 0 ? streamSaveEvtsRef.current : evtsRef.current;
      let _savedEvts = _rawEvts.filter(e=>["source_links","search_results","kb_sources","codeagent_note","tool_start","tool_end","tool_done","tool_error","thinking","thought_done","code_output","file_ready"].includes(e.type) && (e.data?.tool||"")!=="processing");
      const _streamHasFullProductBuild = _eventsHaveDaedalusFullBuild(_savedEvts);
      // Bound the persisted metadata. `thinking` events are cumulative 3KB tail
      // snapshots emitted every ~100 chars — a long reasoning trace persists
      // ~100 overlapping copies (~300KB on one message, reloaded every fetch).
      // Keep only the final thinking snapshot, then cap the whole set.
      {
        let _lastThinkIdx=-1;
        for(let i=_savedEvts.length-1;i>=0;i--){if(_savedEvts[i].type==="thinking"){_lastThinkIdx=i;break;}}
        if(_lastThinkIdx>=0)_savedEvts=_savedEvts.filter((e,i)=>e.type!=="thinking"||i===_lastThinkIdx);
        if(_savedEvts.length>150)_savedEvts=_savedEvts.slice(-150);
      }
      // Pluck run_ids out of the raw event stream so the Daedalus summary can
      // find every run even when older status events are trimmed.
      const _streamRunIds = _runIdsFromEvents(_rawEvts);
      const _msgMeta = (_savedEvts.length || refinementsCount > 0 || _streamRunIds.length) ? {
        ...(_savedEvts.length?{saved_events:_savedEvts}:{}),
        ...(refinementsCount>0?{refinements:refinementsCount}:{}),
        ...(_streamRunIds.length?{run_ids:_streamRunIds}:{}),
        has_full_product_build: _streamHasFullProductBuild,
        in_progress: false,
      } : {in_progress: false};
      const finalFull=replacePersonaPlaceholdersForConversation(full,cv);
      // Auto-play the reply when the Voice "Auto-play replies" toggle is on
      if(ttsAutoplay&&ttsUrl&&finalFull&&!isGhostSend){setTimeout(()=>{try{speak(finalFull,_streamMsgId!=null?_streamMsgId:`auto-${Date.now()}`);}catch{}},80);}
      uConv(cid,c=>{const m=[...(c.messages||[])];m[m.length-1]={...m[m.length-1],role:"assistant",content:finalFull,isS:false,metadata:_msgMeta};return{...c,messages:m};});
      // Save final assistant message state to DB. PATCH the row the backend already
      // created at stream start (preferred — exactly one row per turn). Fall back to
      // POST for older backends or if init/done didn't include a message_id.
      if(isGhostSend){
        // Ghost chats are intentionally local-only: no assistant PATCH/POST,
        // no title generation, no workspace memory suggestion source row.
      }else if(_streamMsgId){
        fetch(`${API}/api/conversations/${cid}/messages/${_streamMsgId}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({content:finalFull,metadata:_msgMeta})}).catch(()=>{});
      }else{
        fetch(`${API}/api/conversations/${cid}/messages`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({role:"assistant",content:finalFull,metadata:_msgMeta})}).catch(()=>{});
      }
      // Auto-generate title after first exchange
      if(!isGhostSend&&autoTitle&&appendUser){const cv2=convs.find(c=>c.id===cid);const curTitle=cv2?.title||"";if(!curTitle||curTitle==="New Chat"||curTitle===appendUser.slice(0,40))fetch(`${API}/api/conversations/${cid}/generate-title`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model:wsModel||""})}).then(r=>r.json()).then(d=>{if(d.title)uConv(cid,{title:d.title});}).catch(()=>{});}
    }catch(e){if(e.name!=="AbortError"){setCurrentRunNotice({type:"failed",label:"Failed",detail:e.message||"Stream failed",at:Date.now()});uConv(cid,c=>{const m=[...(c.messages||[])];m[m.length-1]={...m[m.length-1],role:"assistant",content:`\`\`\`\n⚠ ${e.message}\n\`\`\``,isS:false};return{...c,messages:m};});}}
    setStreaming(false);setAttachments([]);streamingCidRef.current=null;
  };

  const send=async()=>{
    if(streaming||loadingConv||(!inp.trim()&&!attachments.length))return;
    if(attachments.some(a=>a.loading)){notify({type:"warning",text:"Attachment still processing",detail:"Wait for extraction to finish or remove the file."});return;}
    let cid=actId;
    const pendingEffortForSend=!cid&&pendingEffort!==null?pendingEffort:undefined;
    let createdConv=null;
    if(!cid){createdConv=await newChat(true,{returnConv:true});cid=createdConv?.id;if(!cid)return;}
    const cv=createdConv||convs.find(c=>c.id===cid);
    // Route council chats to council sender
    if(cv?.is_council){await sendCouncil();return;}
    // Build content with attachments — full text for model, display version for UI
    let displayContent = inp.trim();
    let modelContent = displayContent;
    const pdfAttachments = [];
    const imageAttachments = []; // {name, dataUrl, mime} for UI render
    const imageBase64s = [];      // raw base64 (no data: prefix) for Ollama images field
    if(attachments.length){
      const pdfParts = [];
      const fileParts = [];
      const imageNames = [];
      for(const a of attachments){
        if(a.type==="image"){
          imageAttachments.push({name:a.name,dataUrl:a.dataUrl,mime:a.mime});
          // Strip "data:image/png;base64," prefix → raw base64 for Ollama
          const b64=(a.dataUrl||"").split(",")[1]||"";
          if(b64) imageBase64s.push(b64);
          imageNames.push(a.name);
        }else if(a.type==="pdf"){
          if(a.error)continue;
          pdfParts.push(`📄 **${a.name}** (${a.pages||"?"} pages):\n${a.content}`);
          pdfAttachments.push({name:a.name,pages:a.pages||0});
        }else{
          fileParts.push(`📎 **${a.name}**:\n\`\`\`\n${a.content}\n\`\`\``);
        }
      }
      const allFileCtx = [...fileParts,...pdfParts].join("\n\n");
      modelContent = modelContent ? `${modelContent}\n\n${allFileCtx}` : allFileCtx;
      // Non-vision-capable models won't see images directly; give them a textual hint.
      if(imageNames.length){
        const imgHint = `[Attached ${imageNames.length} image${imageNames.length>1?"s":""}: ${imageNames.join(", ")}]`;
        modelContent = modelContent ? `${modelContent}\n\n${imgHint}` : imgHint;
      }
      // Non-PDF files still show inline
      if(fileParts.length){
        const nonPdfCtx = fileParts.join("\n\n");
        displayContent = displayContent ? `${displayContent}\n\n${nonPdfCtx}` : nonPdfCtx;
      }
    }
    if(!modelContent.trim()&&!imageBase64s.length){notify({type:"warning",text:"No sendable content",detail:"Retry or remove failed attachments before sending."});return;}
    // Quick search results now stream in via SSE (search_results event with
    // source="quick_search") — same data the chat agent saw, no parallel
    // fetch. Just clear the carousel and flip to loading; the SSE handler
    // populates quickResults when the agent finishes its search.
    if(quickSearch&&modelContent.trim()){
      setSearchLoading(true);setQuickResults([]);setQuickSearchError(null);
    }else{setQuickResults([]);setQuickSearchError(null);}
    const _now=new Date().toISOString();
    const um={role:"user",content:displayContent,_fullContent:modelContent,_images:imageBase64s.length?imageBase64s:undefined,metadata:{pdfs:pdfAttachments.length?pdfAttachments:undefined,images:imageAttachments.length?imageAttachments:undefined},created_at:_now};
    uConv(cid,c=>{const existing=c.messages||[];const hasRealMessages=existing.some(m=>!(m.metadata&&m.metadata.persona_first_message));return{...c,title:!hasRealMessages?(displayContent||pdfAttachments.map(p=>p.name).join(", ")).slice(0,40):c.title,messages:[...existing,um,{role:"assistant",content:"",isS:true,created_at:_now}]};});
    setInp("");setAttachments([]);if(inpRef.current){inpRef.current.style.height="auto";}
    setTimeout(()=>{if(chatScrollRef.current)chatScrollRef.current.scrollTop=chatScrollRef.current.scrollHeight;},0);
    const am=[...(cv?.messages||[]),um];
    const sendOverrides={};
    if(pendingEffortForSend!==undefined)sendOverrides.effort=pendingEffortForSend;
    if(createdConv){sendOverrides.conversation=createdConv;sendOverrides.freshChat=true;}
    await sendMessages(cid, am, um._fullContent||um.content, Object.keys(sendOverrides).length?sendOverrides:undefined);
  };

  const regenerate=async(msgIndex, overrides)=>{
    if(streaming)return;
    const cid=actId;if(!cid)return;
    // Remove the assistant message at msgIndex and re-send everything before it
    uConv(cid,c=>{const m=[...(c.messages||[])];m[msgIndex]={role:"assistant",content:"",isS:true,created_at:new Date().toISOString()};return{...c,messages:m.slice(0,msgIndex+1)};});
    const cv=convs.find(c=>c.id===cid);
    const msgsBeforeRegen=(cv?.messages||[]).slice(0,msgIndex);
    await sendMessages(cid, msgsBeforeRegen, null, overrides);
  };

  const editMessage=async(msgIndex, newContent)=>{
    if(streaming)return;
    const cid=actId;if(!cid)return;
    setEditingMsg(null);
    // Replace the user message and remove everything after it, then re-send
    uConv(cid,c=>{const m=(c.messages||[]).slice(0,msgIndex);const _now=new Date().toISOString();m.push({role:"user",content:newContent,created_at:_now});m.push({role:"assistant",content:"",isS:true,created_at:_now});return{...c,messages:m};});
    const cv=convs.find(c=>c.id===cid);
    const msgsUpToEdit=(cv?.messages||[]).slice(0,msgIndex);
    msgsUpToEdit.push({role:"user",content:newContent});
    await sendMessages(cid, msgsUpToEdit, newContent);
  };

  const saveAssistantEdit=async(msgIndex,newContent)=>{
    if(streaming)return;
    const cid=actId;if(!cid)return;
    setEditingMsg(null);
    const cv=convs.find(c=>c.id===cid);
    const target=cv?.messages?.[msgIndex];
    if(!target)return;
    // Edit in place — no truncation or resend; later turns keep their context
    uConv(cid,c=>{const m=[...(c.messages||[])];if(m[msgIndex])m[msgIndex]={...m[msgIndex],content:newContent};return{...c,messages:m};});
    if(String(cid).startsWith("ghost-"))return; // ghost chats are local-only
    try{
      let dbId=target.id;
      if(!dbId){
        // Resolve the DB row by position, same convention as forkAt
        const full=await(await fetch(`${API}/api/conversations/${cid}`)).json();
        const cand=(full.messages||[])[msgIndex];
        if(cand?.role===target.role)dbId=cand.id;
      }
      if(dbId)await fetch(`${API}/api/conversations/${cid}/messages/${dbId}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({content:newContent})});
      else console.warn("Edited assistant message not persisted: could not resolve DB id");
    }catch(e){console.warn("Could not persist edited assistant message:",e);}
  };

  const delMsg=(msgIndex)=>{
    if(streaming)return;
    const cid=actId;if(!cid)return;
    const cv=convs.find(c=>c.id===cid);
    const removed=cv?.messages?.[msgIndex];
    if(!removed)return;
    uConv(cid,c=>({...c,messages:(c.messages||[]).filter((_,ix)=>ix!==msgIndex)}));
    const tid=Math.random().toString(36).slice(2,9);
    let undone=false;
    const onUndo=()=>{
      undone=true;
      uConv(cid,c=>{const m=[...(c.messages||[])];m.splice(msgIndex,0,removed);return{...c,messages:m};});
      setToasts(ts=>ts.filter(x=>x.id!==tid));
    };
    setToasts(ts=>[...ts.slice(-4),{id:tid,type:"info",text:"Message deleted",action:{label:"Undo",onClick:onUndo}}]);
    setTimeout(()=>{
      setToasts(ts=>ts.filter(x=>x.id!==tid));
      if(!undone&&removed.id){
        fetch(`${API}/api/conversations/${cid}/messages/${removed.id}`,{method:"DELETE"}).catch(()=>{});
      }
    },5000);
  };

  // File upload handler
  const handleFileUpload=async(files)=>{
    // If Coder Bot is active, intercept archive drops and route them to the
    // project upload endpoint instead of treating them as text attachments.
    const isArchive=(f)=>{const n=(f.name||"").toLowerCase();return n.endsWith(".zip")||n.endsWith(".tar")||n.endsWith(".tar.gz")||n.endsWith(".tgz")||n.endsWith(".tar.bz2")||n.endsWith(".tbz2");};
    const isPdf=(f)=>(f.name||"").toLowerCase().endsWith(".pdf");
    const isImage=(f)=>(f.type||"").startsWith("image/")||/\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(f.name||"");
    const coderActive=isCoderPersonaName(act?.persona_name);
    let remaining=files;
    if(coderActive){
      const archives=files.filter(isArchive);
      remaining=files.filter(f=>!isArchive(f));
      if(archives.length){
        if(archives.length>1)console.warn("Multiple archives dropped — only the first will be attached as a Daedalus project.");
        uploadCoderProject(archives[0]);
      }
    }
    const newAttachments=[];
    for(const file of remaining){
      if(isImage(file)){
        if(file.size>20*1024*1024){console.warn(`Skipped ${file.name} — image too large (max 20MB)`);notify({type:"warning",text:"Image skipped",detail:`${file.name} is larger than 20MB.`});continue;}
        try{
          const dataUrl=await new Promise((res,rej)=>{const rd=new FileReader();rd.onload=()=>res(rd.result);rd.onerror=rej;rd.readAsDataURL(file);});
          // Friendly default name for clipboard pastes (browser provides "image.png")
          const niceName=file.name||`screenshot-${new Date().toISOString().replace(/[:.]/g,"-").slice(0,19)}.png`;
          newAttachments.push({name:niceName,dataUrl,type:"image",size:file.size,mime:file.type||"image/png"});
        }catch(e){console.error("Image read failed:",e);notify({type:"error",text:"Image read failed",detail:e.message||String(e)});}
        continue;
      }
      if(isPdf(file)){
        if(file.size>250*1024*1024){console.warn(`Skipped ${file.name} — too large (max 250MB)`);notify({type:"warning",text:"PDF skipped",detail:`${file.name} is larger than 250MB.`});continue;}
        // Add a loading placeholder immediately
        setAttachments(p=>[...p,{name:file.name,content:"",type:"pdf",pages:0,loading:true}]);
        try{
          const fd=new FormData();fd.append("file",file);
          const r=await fetch(`${API}/api/extract-pdf`,{method:"POST",body:fd});
          if(!r.ok){const e=await r.json().catch(()=>({}));throw new Error(e.detail||`HTTP ${r.status}`);}
          const d=await r.json();
          setAttachments(p=>p.map((a,i)=>a.name===file.name&&a.loading?{name:file.name,content:d.text||"No text extracted.",type:"pdf",pages:d.total_pages||0,loading:false}:a));
        }catch(e){
          console.error("PDF extraction failed:",e);
          setAttachments(p=>p.map(a=>a.name===file.name&&a.loading?{name:file.name,content:"",type:"pdf",pages:0,loading:false,error:e.message,file}:a));
          notify({type:"error",text:"PDF extraction failed",detail:file.name});
        }
        continue;
      }
      if(file.size>5*1024*1024){notify({type:"warning",text:"File skipped",detail:`${file.name} is larger than 5MB.`});continue;}
      try{
        const text=await file.text();
        newAttachments.push({name:file.name,content:text.substring(0,20000),type:"text"});
      }catch{
        newAttachments.push({name:file.name,content:`[Binary file: ${file.name}, ${(file.size/1024).toFixed(1)}KB]`,type:"binary"});
      }
    }
    if(newAttachments.length)setAttachments(p=>[...p,...newAttachments]);
  };

  const stop=()=>{
    // Tell the backend to abort any in-flight runs (architect, builder,
    // reviewer, fixer) before we drop the SSE connection. Without this the
    // run's ollama call keeps burning until its 600s timeout and the runs
    // row stays status='running' forever.
    try{
      const rids=_runIdsFromEvents(evtsRef.current||evts);
      for(const rid of rids){
        // Fire-and-forget — we don't await, the backend route is idempotent.
        fetch(`${API}/api/runs/${rid}/cancel`,{method:"POST"}).catch(()=>{});
      }
    }catch{}
    abortR.current?.abort();
    setStreaming(false);
    streamingCidRef.current=null;
    setCurrentRunNotice({type:"stopped",label:"Stopped",detail:"Generation stopped by user",at:Date.now()});
  };

  const openPreview=async(filename,url)=>{
    const isExternal=url.startsWith("http://") || url.startsWith("https://");
    const fn=(filename||"").toLowerCase();
    const isArchive=!isExternal&&(fn.endsWith(".tar.gz")||fn.endsWith(".tgz")||fn.endsWith(".zip")||fn.endsWith(".tar"));
    const ext=isExternal
      ?(()=>{const u=url.toLowerCase();if(u.endsWith(".pdf"))return"pdf";if(u.match(/\.(png|jpg|jpeg|gif|webp|svg)/))return u.match(/\.(png|jpg|jpeg|gif|webp|svg)/)[1];if(u.endsWith(".md"))return"md";return"html";})()
      :isArchive?"archive":(filename||"").split(".").pop().toLowerCase();
    setPreviewFile({filename:filename||url,url,type:ext,isExternal});
    setPreviewContent(null);
    setPreviewError(null);
    setPreviewArchive(null);
    if(isArchive){
      try{
        const safeName=filename.split("/").pop();
        const r=await fetch(`${API}/api/downloads/${encodeURIComponent(safeName)}/contents`);
        if(!r.ok){setPreviewError(`Could not read archive (HTTP ${r.status}).`);return;}
        const d=await r.json();
        if(d.error){setPreviewError(d.error);return;}
        setPreviewArchive(d);
      }catch(e){setPreviewError("Failed to read archive: "+e.message);}
      return;
    }
    if(isExternal){
      // External URL — use proxy for HTML/text content, direct proxy URL for PDF/images
      if(ext==="pdf"||["png","jpg","jpeg","gif","webp","svg"].includes(ext)){
        // Handled by proxy URL in the preview panel renderer
        return;
      }
      try{
        const r=await fetch(`${API}/api/proxy-preview?url=${encodeURIComponent(url)}`);
        if(!r.ok){setPreviewError(`Source unavailable (HTTP ${r.status}).`);return;}
        const text=await r.text();
        setPreviewContent(text);
      }catch(e){setPreviewError("Failed to load source: "+e.message);}
    } else {
      if(["md","markdown","mdx","txt","html","py","js","ts","json","css","sh","rs","go","java","c","cpp","yaml","toml","xml","csv","log","conf","ini","env"].includes(ext)){
        try{
          const r=await fetch(`${API}${url}`);
          if(!r.ok){setPreviewError(`File unavailable (HTTP ${r.status}). It may have been cleaned up.`);return;}
          const text=await r.text();
          setPreviewContent(text);
        }catch(e){setPreviewError("Failed to load file: "+e.message);}
      }
    }
  };
  const closePreview=()=>{setPreviewFile(null);setPreviewContent(null);setPreviewError(null);setPreviewArchive(null);};
  const openArtifact=(artifactId)=>{
    if(artifactId)setArtifactFocusId(artifactId);
    setWsPanel(false);
    setPanel("artifacts");
  };

  const startPreviewDrag=(e)=>{
    e.preventDefault();
    const startX=e.clientX,startW=previewWidth;
    const onMove=(ev)=>{
      const nw=Math.max(260,Math.min(900,startW+(startX-ev.clientX)));
      setPreviewWidth(nw);
    };
    const onUp=()=>{
      localStorage.setItem("hc-preview-w",String(previewWidth));
      window.removeEventListener("mousemove",onMove);
      window.removeEventListener("mouseup",onUp);
    };
    window.addEventListener("mousemove",onMove);
    window.addEventListener("mouseup",onUp);
  };

  const exportChat=()=>{
    try{
      const cv=convs.find(c=>c.id===actId);
      if(!cv)return;
      const lines=[`# ${cv.title||"Chat Export"}`,`*Exported: ${new Date().toLocaleString()}*`,""];
      for(const m of(cv.messages||[])){
        if(m.role==="system")continue;
        lines.push(`## ${m.role==="user"?"You":"Assistant"}`,"",(m.content||""),"");
      }
      const blob=new Blob([lines.join("\n")],{type:"text/markdown"});
      const a=document.createElement("a");
      a.href=URL.createObjectURL(blob);
      a.download=`${(cv.title||"chat").replace(/[^a-z0-9]/gi,"_").toLowerCase()}.md`;
      a.click();
      URL.revokeObjectURL(a.href);
    }catch(e){notify({type:"error",text:"Export failed",detail:e.message||String(e)});}
  };
  const exportChatJSON=()=>{
    try{
      const cv=convs.find(c=>c.id===actId);
      if(!cv)return;
      const data={id:cv.id,title:cv.title,model:cv.model,system_prompt:cv.system_prompt,persona_name:cv.persona_name||null,use_memories:cv.use_memories||"0",created_at:cv.created_at,messages:(cv.messages||[]).filter(m=>m.role!=="system").map(m=>({role:m.role,content:m.content,metadata:m.metadata,created_at:m.created_at}))};
      const blob=new Blob([JSON.stringify(data,null,2)],{type:"application/json"});
      const a=document.createElement("a");
      a.href=URL.createObjectURL(blob);
      a.download=`${(cv.title||"chat").replace(/[^a-z0-9]/gi,"_").toLowerCase()}.json`;
      a.click();
      URL.revokeObjectURL(a.href);
    }catch(e){notify({type:"error",text:"Export failed",detail:e.message||String(e)});}
  };
  const importChatJSON=async(file)=>{
    try{
      const text=await file.text();
      const data=JSON.parse(text);
      if(!data.messages||!data.messages.length){notify({type:"warning",text:"Import skipped",detail:"No messages found in the JSON file."});return;}
      const id=`conv-${Date.now()}-${Math.random().toString(36).slice(2,6)}`;
      const r=await fetch(`${API}/api/conversations`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id,title:data.title||"Imported Chat",model:data.model||models[0]||"",system_prompt:data.system_prompt||""})});
      if(!r.ok)throw new Error(`HTTP ${r.status}`);
      for(const m of data.messages){
        await fetch(`${API}/api/conversations/${id}/messages`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({role:m.role,content:m.content,metadata:m.metadata||null})});
      }
      const cr=await fetch(`${API}/api/conversations/${id}`);
      if(cr.ok){const cv=await cr.json();setConvs(p=>[cv,...p]);setActId(id);setPanel("chat");notify({type:"success",text:"Chat imported",detail:cv.title||"Imported Chat"});}
    }catch(e){console.error("Import failed:",e);notify({type:"error",text:"Import failed",detail:e.message||String(e)});}
  };
  const decodeLatin1=(bytes)=>{try{return new TextDecoder("iso-8859-1").decode(bytes);}catch{return Array.from(bytes,b=>String.fromCharCode(b)).join("");}};
  const decodeBase64Utf8=(text)=>{
    const clean=String(text||"").trim().replace(/\s+/g,"").replace(/-/g,"+").replace(/_/g,"/");
    const padded=clean+"=".repeat((4-clean.length%4)%4);
    const raw=atob(padded);
    return new TextDecoder("utf-8").decode(Uint8Array.from(raw,c=>c.charCodeAt(0)));
  };
  const parseCharacterCardPayload=(text)=>{
    const raw=String(text||"").trim();
    const attempts=[raw];
    try{attempts.push(decodeBase64Utf8(raw));}catch{}
    for(const candidate of attempts){
      if(!candidate)continue;
      try{return JSON.parse(candidate);}catch{}
    }
    throw new Error("No valid character-card JSON found.");
  };
  const scoreCharacterCard=(card,key="")=>{
    const d=card?.data||card||{};
    let score=0;
    const spec=String(card?.spec||d?.spec||"").toLowerCase();
    if(spec.includes("v3"))score+=30;
    else if(spec.includes("v2"))score+=20;
    if(d.name||d.char_name)score+=8;
    for(const k of ["description","personality","scenario","first_mes","first_message","mes_example"])if(d[k])score+=3;
    if(String(key).toLowerCase()==="chara")score+=5;
    if(String(key).toLowerCase().includes("ccv3"))score+=10;
    return score;
  };
  const extractPngCharacterCard=async(file)=>{
    const buf=await file.arrayBuffer();
    const bytes=new Uint8Array(buf);
    const sig=[137,80,78,71,13,10,26,10];
    if(sig.some((v,i)=>bytes[i]!==v))throw new Error("PNG signature not found.");
    const dv=new DataView(buf);
    const chunks=[];
    let pos=8;
    while(pos+12<=bytes.length){
      const len=dv.getUint32(pos,false);
      const type=decodeLatin1(bytes.slice(pos+4,pos+8));
      const start=pos+8,end=start+len;
      if(end>bytes.length)break;
      const data=bytes.slice(start,end);
      if(type==="tEXt"){
        const z=data.indexOf(0);
        if(z>0)chunks.push({key:decodeLatin1(data.slice(0,z)),text:decodeLatin1(data.slice(z+1))});
      }else if(type==="iTXt"){
        let p=0;
        const readNull=()=>{const s=p;while(p<data.length&&data[p]!==0)p++;const out=data.slice(s,p);p++;return out;};
        const key=decodeLatin1(readNull());
        const compressed=data[p++]===1;p++;
        readNull();readNull();
        if(!compressed)chunks.push({key,text:new TextDecoder("utf-8").decode(data.slice(p))});
      }
      pos=end+4;
      if(type==="IEND")break;
    }
    const parsed=[];
    for(const chunk of chunks){
      const key=(chunk.key||"").toLowerCase();
      const plausible=key==="chara"||key.includes("chara")||key.includes("ccv3")||String(chunk.text||"").trim().startsWith("{");
      if(!plausible)continue;
      try{
        const card=parseCharacterCardPayload(chunk.text);
        parsed.push({card,score:scoreCharacterCard(card,chunk.key)});
      }catch{}
    }
    parsed.sort((a,b)=>b.score-a.score);
    if(!parsed.length)throw new Error("No Chub/SillyTavern character data found in PNG metadata.");
    return parsed[0].card;
  };
  const characterBookLore=(book)=>{
    const entries=Array.isArray(book?.entries)?book.entries:Array.isArray(book)?book:[];
    return entries.map(e=>[e.name||e.comment||"",e.content||""].filter(Boolean).join(": ")).filter(Boolean).slice(0,20).join("\n\n");
  };
  const firstCardValue=(...values)=>{
    for(const value of values){
      if(Array.isArray(value)&&value.length)return value;
      if(value&&typeof value==="object"){
        const nested=firstCardValue(value.content,value.text,value.description,value.prompt,value.value);
        if(nested)return nested;
        continue;
      }
      if(cleanCardText(value))return value;
    }
    return "";
  };
  const normalizeCardTags=(...values)=>{
    const tags=[];
    for(const value of values){
      const list=Array.isArray(value)?value:String(value||"").split(",");
      for(const item of list){
        const tag=typeof item==="string"?item:(item?.name||item?.tag||item?.slug||item?.label||"");
        const clean=String(tag||"").trim();
        if(clean&&!tags.some(t=>t.toLowerCase()===clean.toLowerCase()))tags.push(clean);
      }
    }
    return tags;
  };
  const cleanCardText=(v)=>String(v||"").replace(/\r\n?/g,"\n").trim();
  const joinCardParts=(...parts)=>{
    const out=[];
    const seen=new Set();
    for(const part of parts.flat()){
      const text=cleanCardText(part);
      if(!text)continue;
      const key=text.replace(/\s+/g," ").toLowerCase();
      if(seen.has(key))continue;
      seen.add(key);out.push(text);
    }
    return out.join("\n\n").trim();
  };
  const chubSectionKey=(label)=>{
    const k=String(label||"").toLowerCase().replace(/[^a-z0-9]/g,"");
    if(["description","summary","shortdescription","tagline"].includes(k))return "description";
    if(["personality","persona","traits","personalitytraits","behavior","behaviour","speech","voice","speakingstyle"].includes(k))return "personality";
    if(["scenario","setting","context","situation","roleplay","roleplayscenario"].includes(k))return "scenario";
    if(["firstmessage","firstmes","firstmesg","greeting","initialmessage","openingmessage"].includes(k))return "first_message";
    if(["exampledialogue","exampledialogues","examplemessages","mesexample","sampledialogue","dialogueexample"].includes(k))return "example_dialogue";
    if(["systemprompt","posthistoryinstructions","posthistoryinstruction","advancedprompt","jailbreak"].includes(k))return "advanced_prompt";
    if(["appearance","looks","body","physicalappearance","physicaldescription"].includes(k))return "appearance";
    if(["details","character","char","clothing","outfit","age","gender","height","occupation","species","race","likes","dislikes","relationship","relationships","backstory","background","history","lore","worldnotes","worldscenario","creatornotes","notes","forkcleanup","rules","instructions"].includes(k))return "lore";
    return null;
  };
  const splitChubDefinition=(text)=>{
    const out={description:"",personality:"",appearance:"",scenario:"",first_message:"",example_dialogue:"",lore:"",advanced_prompt:"",structured:false};
    const add=(key,value)=>{
      const v=cleanCardText(value);
      if(!v)return;
      out[key]=joinCardParts(out[key],v);
    };
    let current="description";
    for(const rawLine of cleanCardText(text).split("\n")){
      const line=rawLine.trimEnd();
      const trimmed=line.trim();
      if(!trimmed){if(out[current])out[current]+="\n";continue;}
      const colon=trimmed.match(/^([A-Za-z][A-Za-z0-9 _/{}().'-]{1,64})\s*:\s*(.*)$/);
      if(colon){
        const mapped=chubSectionKey(colon[1]);
        if(mapped){
          out.structured=true;
          current=mapped;
          const value=colon[2].trim();
          if(value)add(mapped,mapped==="lore"?`${colon[1].trim()}: ${value}`:value);
          continue;
        }
      }
      const bare=chubSectionKey(trimmed);
      if(bare&&trimmed.length<70){
        out.structured=true;
        current=bare;
        if(bare==="lore")add("lore",trimmed);
        continue;
      }
      add(current,line);
    }
    Object.keys(out).forEach(k=>{if(typeof out[k]==="string")out[k]=cleanCardText(out[k]);});
    return out;
  };
  const importedCardDescription=(raw,name)=>{
    const text=cleanCardText(raw).replace(/\s+/g," ");
    if(!text)return `${name} character card imported from Chub/SillyTavern.`;
    return text;
  };
  const alternateGreetingLore=(greetings)=>{
    if(!Array.isArray(greetings)||!greetings.length)return "";
    return "Alternate greetings:\n"+greetings.map((g,i)=>`${i+1}. ${cleanCardText(g)}`).filter(Boolean).slice(0,12).join("\n\n");
  };
  const inferCardRating=(explicit,tags,...texts)=>{
    if(explicit)return normalizePersonaRating(explicit);
    const hay=[...tags,...texts.map(cleanCardText)].join(" ").toLowerCase();
    if(/\b(nsfw|smut|explicit|adult|unrated|xxx|porn|erotic|sexual)\b/.test(hay))return "Unrated";
    if(/\b(mature|violence|violent|gore|dark)\b/.test(hay))return "R";
    return "PG-13";
  };
  const normalizeCharacterCard=(card,file,avatar)=>{
    const d=card?.data||card||{};
    const fallbackName=(file.name||"Imported Persona").replace(/\.(png|json)$/i,"").replace(/[_-]+/g," ").trim();
    const name=String(firstCardValue(d.name,d.char_name,d.character_name,card?.name,card?.title,fallbackName)||"Imported Persona").trim();
    const tags=normalizeCardTags(d.tags,d.extensions?.tags,d.extensions?.chub?.tags,card?.tags);
    const sourceDescription=firstCardValue(d.description,d.char_persona,d.character_definition,d.definition,d.persona,d.prompt);
    const split=splitChubDefinition(sourceDescription||"");
    const rawDescription=split.structured?firstCardValue(split.description,d.tagline,card?.tagline,d.extensions?.chub?.tagline):firstCardValue(sourceDescription,d.tagline,card?.tagline,d.creatorcomment,d.creator_comment);
    const lore=joinCardParts(
      split.structured?split.lore:"",
      d.creator_notes,
      d.creator_notes_multilingual,
      d.world_scenario,
      alternateGreetingLore(d.alternate_greetings),
      characterBookLore(d.character_book)
    );
    const advanced=joinCardParts(split.advanced_prompt,d.system_prompt,d.post_history_instructions,d.depth_prompt,d.extensions?.depth_prompt);
    const personality=joinCardParts(d.personality,d.char_personality,d.personality_summary,split.personality);
    const scenario=joinCardParts(d.scenario,d.situation,d.context,split.scenario);
    const firstMessage=joinCardParts(firstCardValue(d.first_mes,d.first_message,d.firstMessage,d.greeting,d.greeting_message,d.initial_message),split.first_message);
    const examples=joinCardParts(firstCardValue(d.mes_example,d.example_dialogue,d.example_messages,d.sample_messages,d.sample_dialogue,d.dialogue_examples),split.example_dialogue);
    const persona={
      description:importedCardDescription(rawDescription||d.creatorcomment||d.creator_comment||"",name),
      personality,
      appearance:joinCardParts(d.appearance,d.looks,split.appearance),
      scenario,
      first_message:firstMessage,
      example_dialogue:examples,
      lore,
      tags,
      rating:inferCardRating(d.rating||d.extensions?.rating||d.extensions?.chub?.rating,tags,sourceDescription,personality,scenario,firstMessage,examples,lore),
      thinking_mode:"auto",
      advanced_prompt:advanced,
    };
    const system_prompt=buildPersonaPrompt({name,...persona});
    return {name,base_model:models[0]||"",system_prompt,tool_ids:[],kb_ids:[],parameters:{profile_type:"persona",avatar,description:persona.description,persona}};
  };
  const importCharacterCard=async(file)=>{
    try{
      const isPng=(file.type||"")==="image/png"||/\.png$/i.test(file.name||"");
      const avatar=isPng?await new Promise((res,rej)=>{const rd=new FileReader();rd.onload=()=>res(rd.result);rd.onerror=rej;rd.readAsDataURL(file);}):null;
      const card=isPng?await extractPngCharacterCard(file):JSON.parse(await file.text());
      const mc=normalizeCharacterCard(card,file,avatar);
      const r=await fetch(`${API}/api/model-configs`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(mc)});
      if(!r.ok)throw new Error(`HTTP ${r.status}`);
      const d=await r.json();
      const norm={...d,parameters:normalizeProfileParams(d)};
      setMcs(p=>[norm,...p]);
      setPanel("personas");setProfileTab("personas");setEditMc(norm.id);
      notify({type:"success",text:"Character card imported",detail:norm.name});
    }catch(e){console.error("Character card import failed:",e);notify({type:"error",text:"Character card import failed",detail:e.message||String(e)});}
  };
  const cp=(txt,id)=>{
    // navigator.clipboard only works on HTTPS/localhost — use fallback for HTTP
    if(navigator.clipboard&&window.isSecureContext){
      navigator.clipboard.writeText(txt).then(()=>{setCopied(id);setTimeout(()=>setCopied(null),1500);}).catch(()=>{});
    } else {
      // Fallback: textarea + execCommand
      const ta=document.createElement("textarea");
      ta.value=txt;ta.style.cssText="position:fixed;left:-9999px;top:-9999px";
      document.body.appendChild(ta);ta.select();
      try{document.execCommand("copy");setCopied(id);setTimeout(()=>setCopied(null),1500);}catch{}
      document.body.removeChild(ta);
    }
  };

  // Refresh models from Ollama (single source of truth)
  const refreshModels=async()=>{try{const r=await fetch(`${API}/api/models`);const d=await r.json();const next=d.models||[];setModels(next);setModelDetails(d.model_details||{});try{const last=localStorage.getItem("hc-last-model")||"";if(last&&next.length&&!next.includes(last))localStorage.setItem("hc-last-model",next[0]||"");}catch{}}catch{}};
  const loadHyprfit=async(refresh=false)=>{
    setHyprfitLoading(true);
    try{
      const params=new URLSearchParams({live:"true"});
      if(refresh)params.set("refresh","true");
      const r=await fetch(`${API}/api/models/hyprfit?${params}`);
      const d=await r.json();
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      setHyprfit(d);
      setHyprfitProfile(d.profile||null);
      if(d.categories?.length&&!d.categories.some(c=>c.id===hyprfitCategory))setHyprfitCategory("for_you");
    }catch(e){
      notify({type:"error",text:"HyprFit failed to load",detail:e.message||String(e)});
    }
    setHyprfitLoading(false);
  };
  const saveHyprfitProfile=async()=>{
    if(!hyprfitProfile||hyprfitSaving)return;
    setHyprfitSaving(true);
    try{
      const r=await fetch(`${API}/api/settings`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({model_hardware_profile:hyprfitProfile})});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      notify({type:"success",text:"HyprFit profile saved",duration:2200});
      await loadHyprfit(false);
    }catch(e){
      notify({type:"error",text:"HyprFit profile save failed",detail:e.message||String(e)});
    }
    setHyprfitSaving(false);
  };
  const hyprfitProfileHasHardware=p=>!!(((Number(p?.gpu_count||0)>0)&&(Number(p?.total_vram_gb||0)>0))||p?.unified_memory);
  const hyprfitRescanCopy=(d={})=>{
    const mode=d?.detection_mode||"";
    if(mode==="local_detector")return{type:"success",text:"Local accelerator detected",chip:"local detector",color:t.ok};
    if(mode==="remote_ssh_detector")return{type:"success",text:"Remote hardware scanned",chip:"remote scanned",color:t.ok};
    if(mode==="remote_ssh_unconfigured")return{type:"warning",text:"Scan setup required",chip:"scan setup required",color:t.warm};
    if(mode==="remote_ssh_failed")return{type:"error",text:"Hardware scan failed",chip:"scan failed",color:t.err};
    if(mode==="remote_ollama_unreachable"){
      const hasHardware=hyprfitProfileHasHardware(d?.profile);
      return{type:"warning",text:hasHardware?"Ollama unreachable":"No saved hardware profile",chip:hasHardware?"remote unreachable":"no saved profile",color:t.warm};
    }
    if(mode==="cpu_fallback")return{type:"warning",text:"No accelerator detected",chip:"CPU fallback",color:t.warm};
    return{type:d?.persisted?"success":"info",text:d?.persisted?"HyprFit hardware detected":"HyprFit kept saved profile",chip:d?.persisted?"detected":"saved profile",color:d?.persisted?t.ok:t.mut};
  };
  const rescanHyprfitHardware=async()=>{
    if(hyprfitRescanning)return;
    setHyprfitRescanning(true);
    try{
      const r=await fetch(`${API}/api/models/hyprfit/rescan`,{method:"POST"});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      if(d.profile)setHyprfitProfile(d.profile);
      setHyprfitRescanStatus(d);
      const copy=hyprfitRescanCopy(d);
      notify({type:copy.type,text:copy.text,detail:d.message||"",duration:3600});
      await loadHyprfit(false);
    }catch(e){
      notify({type:"error",text:"HyprFit hardware rescan failed",detail:e.message||String(e)});
    }
    setHyprfitRescanning(false);
  };
  const fillPullFromHyprfit=(name)=>{
    if(!name)return;
    setPullName(name);
    setModelParamsOpen(null);
    setShowModelfile(false);
    setModelsTab("ollama");
    setTimeout(()=>{try{pullInputRef.current?.scrollIntoView({block:"center",behavior:"smooth"});pullInputRef.current?.focus();}catch{}},80);
  };

  const refreshModelProviders=async()=>{
    try{
      const r=await fetch(`${API}/api/model-providers`);
      const d=await r.json();
      const map={};
      (d.providers||[]).forEach(p=>{map[p.provider]=p;});
      setModelProviders(map);
      return map;
    }catch{return modelProviders;}
  };
  const saveModelProvider=async(provider,patch={})=>{
    setProviderBusy(p=>({...p,[provider]:"saving"}));
    try{
      const key=(providerKeys[provider]||"").trim();
      const body={...patch};
      if(key)body.api_key=key;
      const r=await fetch(`${API}/api/model-providers/${provider}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      setModelProviders(p=>({...p,[provider]:d}));
      setProviderKeys(p=>({...p,[provider]:""}));
      await refreshModels();
      notify({type:"success",text:"Provider saved",detail:d.label||provider,duration:2400});
    }catch(e){notify({type:"error",text:"Provider save failed",detail:e.message||String(e)});}
    setProviderBusy(p=>({...p,[provider]:null}));
  };
  const testModelProvider=async(provider)=>{
    setProviderBusy(p=>({...p,[provider]:"testing"}));
    try{
      const key=(providerKeys[provider]||"").trim();
      const r=await fetch(`${API}/api/model-providers/${provider}/test`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(key?{api_key:key}:{})});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      await refreshModelProviders();
      notify({type:"success",text:"Provider test passed",detail:`${d.model_count||0} models available`,duration:2600});
    }catch(e){await refreshModelProviders();notify({type:"error",text:"Provider test failed",detail:e.message||String(e),duration:6000});}
    setProviderBusy(p=>({...p,[provider]:null}));
  };
  const deleteModelProvider=async(provider)=>{
    const ok=await confirmAction({title:"Remove provider key",body:`Remove the saved ${provider==="openai"?"OpenAI":"Anthropic"} API key for this HyprChat user?`,confirmLabel:"Remove Key",tone:"danger"});
    if(!ok)return;
    setProviderBusy(p=>({...p,[provider]:"deleting"}));
    try{
      const r=await fetch(`${API}/api/model-providers/${provider}`,{method:"DELETE"});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      setModelProviders(p=>({...p,[provider]:d}));
      setProviderKeys(p=>({...p,[provider]:""}));
      await refreshModels();
      notify({type:"success",text:"Provider key removed",detail:d.label||provider,duration:2400});
    }catch(e){notify({type:"error",text:"Provider delete failed",detail:e.message||String(e)});}
    setProviderBusy(p=>({...p,[provider]:null}));
  };

  // Delete model and clean up all references (active conv, coder model, workspace model, etc.)
  const deleteModel=async(m)=>{
    const ok=await confirmAction({title:"Delete model",body:`Delete ${m}? This removes the local Ollama model and clears references to it in HyprChat.`,confirmLabel:"Delete Model",tone:"danger"});
    if(!ok)return;
    try{
      const r=await fetch(`${API}/api/models/${encodeURIComponent(m)}`,{method:"DELETE"});
      if(!r.ok){const d=await r.json().catch(()=>({}));notify({type:"error",text:"Model delete failed",detail:d.detail||r.statusText});return;}
      // Refresh model list first to get updated available models
      const mr=await fetch(`${API}/api/models`).catch(()=>null);
      let newModels=[];
      if(mr&&mr.ok){const md=await mr.json();newModels=md.models||[];setModels(newModels);setModelDetails(md.model_details||{});}
      const fallback=newModels[0]||"";
      // Update any conversations using the deleted model
      setConvs(prev=>prev.map(c=>{
        if(c.model===m){
          const patch={model:fallback};
          if(!c.id.startsWith("l-")&&!c.id.startsWith("ghost-"))fetch(`${API}/api/conversations/${c.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(patch)}).catch(()=>{});
          return{...c,...patch};
        }
        return c;
      }));
      // Clear planning/coder model if it was the deleted model
      if(planningModel===m){setPlanningModel("");}
      if(coderModel===m){setCoderModel("");}
      // Coder Bot v2 per-agent overrides — clear if pinned to the deleted model
      if(architectModel===m){setArchitectModel("");}
      if(reviewerModel===m){setReviewerModel("");}
      if(acceptanceModel===m){setAcceptanceModel("");}
      if(builderModel===m){setBuilderModel("");}
      if(fixerModel===m){setFixerModel("");}
      if(qaModel===m){setQaModel("");}
      // Clear workspace model if it was the deleted model
      if(wsModel===m){setWsModel(fallback);}
      try{if(localStorage.getItem("hc-last-model")===m)localStorage.setItem("hc-last-model",fallback);}catch{}
      if(modelParamsOpen===m)setModelParamsOpen(null);
      notify({type:"success",text:"Model deleted",detail:m});
    }catch(e){notify({type:"error",text:"Model delete failed",detail:e.message});}
  };
  const patchToolModel=async(m,successText="Modelfile patched for tool calling.")=>{
    if(!m||makingToolModel)return;
    const activityId=addActivity({name:`Enable tools: ${m}`,kind:"model",message:"Patching modelfile...",pct:20});
    setMakingToolModel(m);
    try{
      const r=await fetch(`${API}/api/models/${encodeURIComponent(m)}/create-tool-model`,{method:"POST"});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||r.statusText||`HTTP ${r.status}`);
      updateActivity(activityId,{status:"done",message:"Tool calling enabled",pct:100});
      notify({type:"success",text:"Tool calling enabled",detail:successText});
    }catch(e){
      updateActivity(activityId,{status:"error",error:e.message,message:"Patch failed"});
      notify({type:"error",text:"Tool patch failed",detail:e.message});
    }finally{
      setMakingToolModel(null);
    }
  };

  // Pull model — uses shared downloads UI
  const pullModel=async()=>{
    if(!pullName.trim())return;
    const dlId=`pull-${Date.now()}`;const dlName=pullName.trim();
    setDownloads(p=>[...p,{id:dlId,name:dlName,kind:"model",status:"creating",pct:0,message:"Pulling from Ollama...",error:null,completedBytes:0,totalBytes:0,startTime:Date.now(),speed:0,eta:0,retry:()=>{setPullName(dlName);notify({type:"info",text:"Model name restored",detail:"Press Pull to retry.",duration:4000});}}]);
    setDownloadsExpanded(true);setPullName("");setPullProg(null);
    const updDl=upd=>setDownloads(p=>p.map(d=>d.id===dlId?{...d,...upd}:d));
    try{
      const res=await fetch(`${API}/api/models/pull`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:dlName})});
      if(!res.ok){const msg=`Server error: ${res.status} ${res.statusText}`;updDl({status:"error",error:msg});notify({type:"error",text:"Model pull failed",detail:msg});return;}
      const rdr=res.body.getReader(),dec=new TextDecoder();let buf="";
      let lastBytes=0,lastTime=Date.now(),lastSpeedUpdate=0,curSpeed=0,curEta=0;
      while(true){
        const{done,value}=await rdr.read();if(done)break;
        buf+=dec.decode(value,{stream:true});
        const lines=buf.split("\n");buf=lines.pop()||"";
        for(const ln of lines){
          if(!ln.startsWith("data: "))continue;
          try{const d=JSON.parse(ln.slice(6));
            if(d.error||d.status==="error"){const msg=d.error||d.message||"Unknown error";updDl({status:"error",error:msg});notify({type:"error",text:"Model pull failed",detail:msg});continue;}
            if(d.status==="success"||d.status==="done"){
              updDl({status:"done",pct:100,message:"Done!"});
              notify({type:"success",text:"Model pulled",detail:dlName});
              refreshModels();
              continue;
            }
            const completed=d.completed||0,total=d.total||0;
            const pct=d.pct||(total>0?Math.round(completed/total*100):0);
            if(completed>0&&total>0){
              const now=Date.now();
              if(!lastSpeedUpdate||now-lastSpeedUpdate>=1000){
                if(completed>lastBytes){
                  const dt=(now-lastTime)/1000;
                  curSpeed=dt>0?(completed-lastBytes)/dt:curSpeed;
                  if(curSpeed>0&&total>completed)curEta=Math.round((total-completed)/curSpeed);
                  else curEta=0;
                }
                lastBytes=completed;lastTime=now;lastSpeedUpdate=now;
              }
              updDl({status:"downloading",pct,message:d.message||(d.status+(pct?` ${pct}%`:"")),completedBytes:completed,totalBytes:total,speed:curSpeed,eta:curEta});
            }else if(d.status==="downloading"||d.status==="creating"){
              updDl({status:"creating",pct,message:d.message||d.status||"Preparing..."});
            }
          }catch{}}
      }
    }catch(e){updDl({status:"error",error:e.message});notify({type:"error",text:"Model pull failed",detail:e.message});}
  };

  // HuggingFace helpers
  const hfDoSearch=async(q,ggufOnly=hfGgufOnly)=>{
    if(!q.trim())return;
    setHfLoading(true);setHfResults([]);setHfSelected(null);setHfModelInfo(null);setHfReadme(null);
    try{
      const r=await fetch(`${API}/api/hf/search?${new URLSearchParams({q,gguf_only:ggufOnly,limit:24})}`);
      const d=await r.json();setHfResults(Array.isArray(d)?d:[]);
    }catch{setHfResults([]);}
    setHfLoading(false);
  };
  // Strip HTML tags from HuggingFace README content (badges, img, div, etc.)
  const hfCleanReadme=(text)=>{
    if(!text)return text;
    // Preserve code blocks, strip HTML outside them
    const parts=text.split(/(```[\s\S]*?```)/g);
    return parts.map((p,i)=>{
      if(i%2===1)return p;
      return p
        .replace(/<img[^>]*>/gi,"")  // remove images
        .replace(/<a[^>]*>([\s\S]*?)<\/a>/gi,"$1")  // unwrap anchors
        .replace(/<\/?(div|span|p|center|details|summary|br|strong|em|code|pre|table|tr|td|th|thead|tbody|ul|ol|li|h[1-6]|b|i)[^>]*>/gi,"")
        .replace(/<[^>]{1,200}>/g,"")  // strip remaining short tags
        .replace(/&nbsp;/g," ").replace(/&lt;/g,"<").replace(/&gt;/g,">").replace(/&amp;/g,"&").replace(/&quot;/g,'"')
        .replace(/\[!\[.*?\]\(.*?\)\]\(.*?\)/g,"")  // strip badge links [![...](...)](...)
        .replace(/!\[.*?\]\(.*?\)/g,"");  // strip remaining images ![](...)
    }).join("");
  };
  const hfSelectModel=async(model)=>{
    setHfSelected(model);setHfModelInfo(null);setHfReadme(null);setHfSelectedFiles([]);
    setModelParamsOpen(null);
    const repoId=model.id;
    const defaultName=repoId.split("/").pop().toLowerCase().replace(/[^a-z0-9\-:.]/g,"-").slice(0,60);
    setHfDownloadName(defaultName);
    // Fetch model info and README in parallel
    setHfReadmeLoading(true);
    const [infoR,readmeR]=await Promise.allSettled([
      fetch(`${API}/api/hf/model?repo_id=${encodeURIComponent(repoId)}`).then(r=>r.json()),
      fetch(`${API}/api/hf/readme?repo_id=${encodeURIComponent(repoId)}`).then(r=>r.json()),
    ]);
    if(infoR.status==="fulfilled")setHfModelInfo(infoR.value);
    if(readmeR.status==="fulfilled")setHfReadme(hfCleanReadme(readmeR.value?.content||""));
    setHfReadmeLoading(false);
  };
  const hfCompatibilityReport=(model,info,readme,selectedFiles=[])=>{
    const files=info?.gguf_files||[];
    const selected=files.filter(f=>selectedFiles.includes(f.name));
    const text=[model?.id||"",model?.pipeline_tag||"",(model?.tags||[]).join(" "),readme||"",files.map(f=>f.name).join(" ")].join(" ").toLowerCase();
    const issues=[];
    const add=(level,label,detail)=>{if(!issues.some(i=>i.label===label))issues.push({level,label,detail});};
    const has=rx=>rx.test(text);
    if(info&&files.length===0)add("bad","No GGUF files","This repo does not expose Ollama-loadable GGUF files.");
    if(has(/not supported in ollama|ollama[^.\n]{0,80}not supported|requires[^.\n]{0,80}llama\.cpp|llama\.cpp[^.\n]{0,80}only|requires[^.\n]{0,80}pull request|custom architecture/))add("bad","Runtime support warning","The README suggests this may require a specific runtime or newer loader support.");
    if(has(/gated deltanet|linear attention|state space|hybrid architecture|hybrid[^.\n]{0,50}attention|\bssm\b|\bmamba\b|\brwkv\b|\bjamba\b/))add("warn","Architecture risk","Download may succeed while the local Ollama loader fails to load the blob.");
    if(files.some(f=>/^mmproj/i.test(f.name))||has(/\bmmproj\b|vision encoder|multimodal|vision support|image\/video|image-to-text|image text/))add("warn","Vision/mmproj package","Multimodal models can need projector files and newer Ollama support; text-only loading may still fail.");
    if(files.some(f=>/-\d{5}-of-\d{5}\.gguf$/i.test(f.name)))add("info","Split GGUF","Select every part in the set before downloading.");
    if(selected.length){
      const selectedSize=selected.reduce((a,f)=>a+(f.size||0),0);
      if(selected.every(f=>/^mmproj/i.test(f.name)))add("bad","Projector only","A mmproj file is not a standalone chat model; select a main GGUF too.");
      if(selected.some(f=>/^mmproj/i.test(f.name))&&selected.some(f=>!/^mmproj/i.test(f.name)))add("info","Includes projector","This will register both main GGUF and projector blobs when the runtime supports them.");
      if(selected.some(f=>/\b(BF16|F16|F32)\b/i.test(f.name)))add("warn","Large full-precision file","BF16/F16/F32 downloads are large and much more likely to exceed VRAM.");
      if(selectedSize>42*1024*1024*1024)add("warn","Very large selection",`${fmtSize(selectedSize)} selected; this may exceed available VRAM even if it downloads.`);
      else if(selectedSize>24*1024*1024*1024)add("info","Large selection",`${fmtSize(selectedSize)} selected; load time and VRAM pressure may be high.`);
    }
    const level=issues.some(i=>i.level==="bad")?"bad":issues.some(i=>i.level==="warn")?"warn":"ok";
    return {level,issues};
  };
  const hfDownload=async()=>{
    if(!hfSelected||!hfSelectedFiles.length)return;
    const dlId=`dl-${Date.now()}`;
    const dlName=hfDownloadName.trim();
    const totalSize=(hfModelInfo?.gguf_files||[]).filter(f=>hfSelectedFiles.includes(f.name)).reduce((a,f)=>a+(f.size||0),0);
    setHfDownloading(true);
    setDownloads(p=>[...p,{id:dlId,name:dlName||hfSelectedFiles[0],kind:"model",status:"creating",pct:0,message:"Contacting Ollama...",error:null,completedBytes:0,totalBytes:totalSize,startTime:Date.now(),speed:0,eta:0,retry:()=>{notify({type:"info",text:"Retry from HuggingFace tab",detail:"The same file selection is still available if the model panel has not changed.",duration:5000});}}]);
    setDownloadsExpanded(true);
    const updDl=upd=>setDownloads(p=>p.map(d=>d.id===dlId?{...d,...upd}:d));
    try{
      const res=await fetch(`${API}/api/hf/download`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({repo_id:hfSelected.id,filenames:hfSelectedFiles,model_name:dlName})});
      if(!res.ok){const msg=`Server error: ${res.status} ${res.statusText}`;updDl({status:"error",error:msg});notify({type:"error",text:"HuggingFace download failed",detail:msg});setHfDownloading(false);return;}
      const rdr=res.body.getReader(),dec=new TextDecoder();let buf="";
      let lastBytes=0,lastTime=Date.now(),lastSpeedUpdate=0,curSpeed=0,curEta=0;
      while(true){
        const{done,value}=await rdr.read();if(done)break;
        buf+=dec.decode(value,{stream:true});
        const lines=buf.split("\n");buf=lines.pop()||"";
        for(const ln of lines){
          if(!ln.startsWith("data: "))continue;
          try{
            const d=JSON.parse(ln.slice(6));
            if(d.status==="error"){updDl({status:"error",error:d.message});notify({type:"error",text:"HuggingFace download failed",detail:d.message});}
            else if(d.status==="done"){
              updDl({status:"done",pct:100,message:d.message||"Done!"});
              notify({type:"success",text:"Model downloaded",detail:dlName||hfSelectedFiles[0]});
              refreshModels();
            }else if(d.status==="downloading"){
              const now=Date.now();
              const completed=d.completed||0,total=d.total||0;
              // Throttle speed/ETA calc to about once per second.
              if(completed>0&&(!lastSpeedUpdate||now-lastSpeedUpdate>=1000)){
                if(completed>lastBytes){
                  const dt=(now-lastTime)/1000;
                  curSpeed=dt>0?(completed-lastBytes)/dt:curSpeed;
                  if(curSpeed>0&&total>completed)curEta=Math.round((total-completed)/curSpeed);
                  else curEta=0;
                }
                lastBytes=completed;lastTime=now;lastSpeedUpdate=now;
              }
              updDl({status:"downloading",pct:d.pct||0,message:d.message,completedBytes:completed,totalBytes:total||totalSize,speed:curSpeed,eta:curEta});
            }else if(d.message)updDl({message:d.message,status:d.status||"creating"});
          }catch{}
        }
      }
    }catch(e){updDl({status:"error",error:e.message});notify({type:"error",text:"HuggingFace download failed",detail:e.message});}
    setHfDownloading(false);
  };
  const fmtSize=(bytes)=>{if(!bytes)return"—";if(bytes>1e9)return`${(bytes/1e9).toFixed(1)} GB`;if(bytes>1e6)return`${(bytes/1e6).toFixed(0)} MB`;return`${(bytes/1e3).toFixed(0)} KB`;};
  const capTags=(n)=>{const b=(n||"").toLowerCase();const caps=[];if(b.match(/embed/))caps.push({label:"Embed",emoji:"🔢",color:"#9b59b6"});if(b.match(/llava|vision|[\-:]vl$|[\-:]vl[\-:]/))caps.push({label:"Vision",emoji:"👁",color:"#e67e22"});if(b.match(/coder|codestral|starcoder|deepseek-coder/))caps.push({label:"Code",emoji:"💻",color:"#2ecc71"});if(!b.match(/embed/)&&b.match(/qwen|llama3|llama-3|mistral|mixtral|command|hermes|deepseek|phi3|phi-3|wizardlm|gemma|llama3\./))caps.push({label:"Tools",emoji:"🔧",color:"#3498db"});if(b.match(/mixtral|moe|dbrx|switch|jamba|arctic/))caps.push({label:"MoE",emoji:"🔀",color:"#f39c12"});if(b.match(/qwen3(?!\.5)|deepseek-r1|r1-|thinking|reflection|reason/i)&&!b.match(/qwen3\.5/))caps.push({label:"Thinking",emoji:"💭",color:"#a78bfa"});if(b.match(/abliterated|uncensored|unfiltered|dolphin/))caps.push({label:"Uncensored",emoji:"🔓",color:"#ef4444"});if(b.match(/instruct|chat|it(?:$|[\-:])/))caps.push({label:"Instruct",emoji:"📝",color:"#64748b"});return caps;};
  const quantLabel=(filename)=>{if(!filename)return null;const m=filename.match(/[_\-\.](Q[0-9]_[A-Z_]+|[FQIB][0-9]+(?:_[A-Z0-9]+)?|fp16|bf16|f32|f16|int[48])/i);return m?m[1].toUpperCase():null;};
  const quantColor=(q)=>{if(!q)return"#888";const l=q.toLowerCase();if(l.match(/^(f32|fp32)$/))return"#ef4444";if(l.match(/^(f16|fp16|bf16)$/))return"#f97316";if(l.match(/^q8/))return"#eab308";if(l.match(/^q6/))return"#84cc16";if(l.match(/^q5/))return"#22c55e";if(l.match(/^q4/))return"#06b6d4";if(l.match(/^q3|^q2|^iq/))return"#8b5cf6";return"#94a3b8";};
  const isMultiPart=(files)=>files.some(f=>/\d{5}-of-\d{5}\.gguf$/i.test(f.name));
  const getPartGroups=(files)=>{
    const groups={};
    files.forEach(f=>{
      const m=f.name.match(/^(.+)-(\d{5})-of-(\d{5})\.gguf$/i);
      if(m){const key=`${m[1]}(${m[3]} parts)`;if(!groups[key])groups[key]=[];groups[key].push(f);}
      else{groups[f.name]=[f];}
    });
    return groups;
  };

  // Markdown
  const inlineParse=(text,opts={})=>{
    if(!text)return null;
    const printMode=!!opts.printMode;
    const mathish=v=>/\\[a-zA-Z]+|[\^_=+\-*/{}]/.test(v||"");
    const namedColorRe=/^(black|silver|gray|grey|white|maroon|red|purple|fuchsia|magenta|green|lime|olive|yellow|navy|blue|teal|aqua|cyan|orange|aliceblue|rebeccapurple|transparent)$/i;
    const colorish=v=>/^#[0-9a-fA-F]{3,8}$/.test(v||"")||/^(rgba?|hsla?)\(/i.test(v||"")||namedColorRe.test(v||"");
    // Split by: math, keyboard keys, inline-code, images, bold+link, bold, italic(*), italic(_), markdown link, bare URL, double-quoted strings (10+ chars)
    return text.split(/(`\$(?=[^`\n]*?(?:\\[a-zA-Z]+|[\^_=+\-*/{}]))[^`\n]+?\$`|(?<![\\$])\$(?![\d\s])(?=[^$\n]*?(?:\\[a-zA-Z]+|[\^_=+\-*/{}]))[^$\n]+?(?<!\\)\$|<kbd>[^<]+<\/kbd>|&lt;kbd&gt;(?:(?!&lt;\/kbd&gt;).)+&lt;\/kbd&gt;|`[^`]+`|!\[[^\]]*\]\([^)]+\)|\*\*\[[^\]]+\]\([^)]+\)\*\*|\*\*[^*\n]+\*\*|\*[^*\n]+\*|_[^_\n]+_|\[[^\]]+\]\([^)]+\)|https?:\/\/[^\s"'<>\])\},]+|"[^"\n]{10,}"|rgba?\([^)]+\)|hsla?\([^)]+\)|#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6})\b|[^]+)/g).map((s,k)=>{
      if(!s)return null;
      const imgMatch=s.match(/^!\[([^\]]*)\]\(([^)]+)\)$/);
      const codeText=(s.startsWith("`")&&s.endsWith("`")&&s.length>1)?s.slice(1,-1):null;
      const codeMath=s.match(/^`\$([\s\S]+)\$`$/);
      const bareMath=s.match(/^\$([\s\S]+)\$$/);
      const kbdSource=codeText||s;
      const kbdMatch=kbdSource.match(/^<kbd>([\s\S]+)<\/kbd>$/i)||kbdSource.match(/^&lt;kbd&gt;([\s\S]+)&lt;\/kbd&gt;$/i);
      const decodeInlineHtml=v=>String(v||"").replace(/&lt;/g,"<").replace(/&gt;/g,">").replace(/&amp;/g,"&").replace(/&quot;/g,'"').replace(/&#39;/g,"'");
      if(codeMath&&mathish(codeMath[1]))return <InlineMath key={k} code={codeMath[1]} raw={s} theme={t} font={font} printMode={printMode}/>;
      if(bareMath&&mathish(bareMath[1]))return <InlineMath key={k} code={bareMath[1]} raw={s} theme={t} font={font} printMode={printMode}/>;
      if(kbdMatch){
        const kv=decodeInlineHtml(kbdMatch[1]);
        const kbdStyle=printMode
          ? {display:"inline-block",padding:"0 4px",border:"1px solid #d7dde8",borderRadius:3,background:"#f8fafc",fontFamily:font,fontSize:"0.82em",color:"#364152"}
          : {display:"inline-block",padding:"1px 6px",background:`linear-gradient(180deg,${t.surface}EE,${t.bgDeep})`,border:`1px solid ${t.brd}AA`,borderBottomWidth:2,borderRadius:4,fontSize:"0.82em",fontFamily:font,color:t.text,boxShadow:`0 1px 0 ${t.brd}88`,verticalAlign:"baseline",margin:"0 1px",minWidth:14,textAlign:"center",lineHeight:1.4};
        return <kbd key={k} style={kbdStyle}>{kv}</kbd>;
      }
      if(codeText&&colorish(codeText))return <InlineColorSwatch key={k} value={codeText} theme={t} font={font} printMode={printMode}/>;
      if(printMode){
        const linkStyle={color:"#175cd3",textDecoration:"none",overflowWrap:"anywhere"};
        if(imgMatch){const[,alt,src]=imgMatch;return <span key={k} className="image-note">Image: {alt||src}</span>;}
        if(s.startsWith("`")&&s.endsWith("`")&&s.length>1)return <code key={k} style={{background:"#f1f5f9",border:"1px solid #e2e8f0",borderRadius:3,padding:"0 3px",fontFamily:font,fontSize:"0.9em",color:"#334155"}}>{s.slice(1,-1)}</code>;
        if(s.charCodeAt(0)===0xE010){
          const inner=s.slice(1,-1);const ci=inner.indexOf(":");
          const pref=inner.slice(0,ci),num=inner.slice(ci+1);
          if(pref==="cite")return <sup key={k} style={{fontSize:"0.75em",lineHeight:0,margin:"0 1px"}}>[{num}]</sup>;
          return <sup key={k} style={{fontSize:"0.75em",lineHeight:0,margin:"0 1px"}}><a href={`#fn-${pref}-${num}`} style={{...linkStyle,fontWeight:700}}>{num}</a></sup>;
        }
        const boldLinkMatch=s.match(/^\*\*\[([^\]]+)\]\(([^)]+)\)\*\*$/);
        if(boldLinkMatch){const[,lt,lu]=boldLinkMatch;return <strong key={k}><a href={lu.startsWith("/api/downloads/")?`${API}${lu}`:lu} target="_blank" rel="noopener" style={linkStyle}>{lt}</a></strong>;}
        if(s.startsWith("**")&&s.endsWith("**")&&s.length>4)return <strong key={k} style={{color:"#161b22",fontWeight:700}}>{s.slice(2,-2)}</strong>;
        if((s.startsWith("*")&&s.endsWith("*")&&s.length>2)||(s.startsWith("_")&&s.endsWith("_")&&s.length>2))return <em key={k} style={{fontStyle:"italic"}}>{s.slice(1,-1)}</em>;
        const linkMatch=s.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
        if(linkMatch){const[,lt,lu]=linkMatch;return <a key={k} href={lu.startsWith("/api/downloads/")?`${API}${lu}`:lu} target="_blank" rel="noopener" style={linkStyle}>{lt}</a>;}
        if(s.match(/^https?:\/\//)){
          const lu=s.replace(/[.,;:!?)]+$/,"");const trail=s.slice(lu.length);
          return <span key={k}><a href={lu} target="_blank" rel="noopener" style={linkStyle}>{lu}</a>{trail}</span>;
        }
        if(s.startsWith('"')&&s.endsWith('"')&&s.length>11)return <span key={k} style={{color:"#92400e",fontStyle:"italic"}}>{s}</span>;
        if(s.startsWith("<kbd>")&&s.endsWith("</kbd>"))return <kbd key={k} style={{display:"inline-block",padding:"0 4px",border:"1px solid #d7dde8",borderRadius:3,background:"#f8fafc",fontFamily:font,fontSize:"0.82em",color:"#364152"}}>{s.slice(5,-6)}</kbd>;
        if(s.match(/^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/)||s.match(/^(rgba?|hsla?)\(/)){
          return <span key={k} style={{display:"inline-flex",alignItems:"center",gap:4}}><span style={{width:9,height:9,borderRadius:2,background:s,border:"1px solid #d7dde8",flexShrink:0}}/><code style={{fontSize:"0.85em",color:"#334155",background:"transparent",border:0,padding:0,fontFamily:font}}>{s}</code></span>;
        }
        return s;
      }
      if(imgMatch){const[,alt,src]=imgMatch;
        // Force any third-party image URL through our proxy: privacy (no IP/cookies leaked
        // to NYT/Wikipedia/etc), hotlink bypass (proxy sends domain-matched Referer), and
        // mixed-content fix. /api/img-proxy URLs and same-origin /api/ URLs pass through.
        const finalSrc = proxiedImageUrl(src);
        return <img key={k} src={finalSrc} alt={alt||""} referrerPolicy="no-referrer" loading="lazy" style={{maxWidth:"100%",maxHeight:380,borderRadius:8,display:"block",margin:"6px 0",border:`1px solid ${t.brd}22`}} onError={e=>e.target.style.display="none"}/>;}
      if(s.startsWith("`")&&s.endsWith("`")&&s.length>1)
        return <code key={k} style={{background:`${t.surface}CC`,padding:"1px 5px",borderRadius:3,fontFamily:font,fontSize:"0.88em",color:t.warm}}>{s.slice(1,-1)}</code>;
      if(s.charCodeAt(0)===0xE010){
        const inner=s.slice(1,-1);const ci=inner.indexOf(":");
        const pref=inner.slice(0,ci),num=inner.slice(ci+1);
        if(pref==="cite")return <CitationChip key={k} n={num} src={(opts.citations||[])[parseInt(num,10)-1]} theme={t} font={font}/>;
        return <sup key={k} style={{fontSize:"0.75em",lineHeight:0,margin:"0 1px"}}><a href={`#fn-${pref}-${num}`} onClick={e=>{e.preventDefault();const el=document.getElementById(`fn-${pref}-${num}`);if(el)el.scrollIntoView({behavior:"smooth",block:"center"});}} style={{color:t.acc,textDecoration:"none",padding:"0 3px",borderRadius:3,background:`${t.acc}18`,fontWeight:700}}>{num}</a></sup>;
      }
      const boldLinkMatch=s.match(/^\*\*\[([^\]]+)\]\(([^)]+)\)\*\*$/);
      if(boldLinkMatch){
        const [,lt,lu]=boldLinkMatch;
        if(lu.startsWith("/api/downloads/")){
          const fn=lu.split("/").pop();
          return <span key={k} style={{display:"inline-flex",alignItems:"center",gap:0,margin:"6px 0"}}>
            <a href={`${API}${lu}`} download={fn} style={{display:"inline-flex",alignItems:"center",gap:6,padding:"8px 16px",background:`${t.ok}15`,border:`1px solid ${t.ok}40`,borderRadius:"10px 0 0 10px",color:t.ok,textDecoration:"none",fontFamily:font,fontSize:13,fontWeight:700,cursor:"pointer",transition:"all .15s"}}>
              <span style={{fontSize:18}}>📁</span><span>{fn}</span><span style={{fontSize:10,opacity:.6,marginLeft:4}}>⬇</span>
            </a>
            <button onClick={()=>openPreview(fn,lu)} style={{padding:"8px 10px",background:`${t.acc}15`,border:`1px solid ${t.acc}40`,borderRadius:"0 10px 10px 0",borderLeft:"none",color:t.acc,cursor:"pointer",fontSize:14,fontFamily:font}}>👁</button>
          </span>;
        }
        if(lu.startsWith("http://") || lu.startsWith("https://")){
          const qsSrc=_quickSourceForUrl(lu,opts.quickSearch?.results);
          if(qsSrc)return <QuickSourceInlineChip key={k} href={lu} label={lt} source={qsSrc} t={t} font={font} onPreview={openPreview}/>;
          const hn=(()=>{try{return new URL(lu).hostname.replace("www.","");}catch{return "";}})();
          return <span key={k} style={{display:"inline-flex",alignItems:"center",gap:0,margin:"2px 0",verticalAlign:"middle"}}>
            <a href={lu} target="_blank" rel="noopener" style={{display:"inline-flex",alignItems:"center",gap:5,padding:"4px 10px",background:`${t.warm}10`,border:`1px solid ${t.warm}33`,borderRadius:"8px 0 0 8px",color:t.warm,textDecoration:"none",fontSize:11,fontWeight:700,maxWidth:280,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>
              <img src={`https://www.google.com/s2/favicons?domain=${hn}&sz=16`} style={{width:12,height:12,borderRadius:2,flexShrink:0}} alt="" onError={e=>e.target.style.display="none"}/>
              <span>{lt.length>45?lt.slice(0,45)+"...":lt}</span>
            </a>
            <button onClick={()=>openPreview(lt,lu)} title="Preview" style={{padding:"4px 7px",background:`${t.acc}12`,border:`1px solid ${t.acc}33`,borderRadius:"0 8px 8px 0",borderLeft:"none",color:t.acc,cursor:"pointer",fontSize:11,display:"flex",alignItems:"center"}}>👁</button>
          </span>;
        }
        return <a key={k} href={lu} target="_blank" rel="noopener" style={{color:t.acc,textDecoration:"none",borderBottom:`1px solid ${t.acc}40`,fontWeight:700}}>{lt}</a>;
      }
      if(s.startsWith("**")&&s.endsWith("**")&&s.length>4)return <strong key={k} style={{color:t.text,fontWeight:600}}>{s.slice(2,-2)}</strong>;
      if((s.startsWith("*")&&s.endsWith("*")&&s.length>2)||(s.startsWith("_")&&s.endsWith("_")&&s.length>2))
        return <em key={k} style={{fontStyle:"italic"}}>{s.slice(1,-1)}</em>;
      const linkMatch=s.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      if(linkMatch){
        const [,lt,lu]=linkMatch;
        if(lu.startsWith("/api/downloads/")){
          const fn=lu.split("/").pop();
          return <span key={k} style={{display:"inline-flex",alignItems:"center",gap:0,margin:"6px 0"}}>
            <a href={`${API}${lu}`} download={fn} style={{display:"inline-flex",alignItems:"center",gap:6,padding:"8px 16px",background:`${t.ok}15`,border:`1px solid ${t.ok}40`,borderRadius:"10px 0 0 10px",color:t.ok,textDecoration:"none",fontFamily:font,fontSize:13,fontWeight:700,cursor:"pointer",transition:"all .15s"}}>
              <span style={{fontSize:18}}>📁</span><span>{fn}</span><span style={{fontSize:10,opacity:.6,marginLeft:4}}>⬇</span>
            </a>
            <button onClick={()=>openPreview(fn,lu)} style={{padding:"8px 10px",background:`${t.acc}15`,border:`1px solid ${t.acc}40`,borderRadius:"0 10px 10px 0",borderLeft:"none",color:t.acc,cursor:"pointer",fontSize:14,fontFamily:font}}>👁</button>
          </span>;
        }
        if(lu.startsWith("http://") || lu.startsWith("https://")){
          const qsSrc=_quickSourceForUrl(lu,opts.quickSearch?.results);
          if(qsSrc)return <QuickSourceInlineChip key={k} href={lu} label={lt} source={qsSrc} t={t} font={font} onPreview={openPreview}/>;
          const hn=(()=>{try{return new URL(lu).hostname.replace("www.","");}catch{return "";}})();
          return <span key={k} style={{display:"inline-flex",alignItems:"center",gap:0,margin:"2px 0",verticalAlign:"middle"}}>
            <a href={lu} target="_blank" rel="noopener" style={{display:"inline-flex",alignItems:"center",gap:5,padding:"4px 10px",background:`${t.warm}10`,border:`1px solid ${t.warm}33`,borderRadius:"8px 0 0 8px",color:t.warm,textDecoration:"none",fontSize:11,fontWeight:600,maxWidth:280,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>
              <img src={`https://www.google.com/s2/favicons?domain=${hn}&sz=16`} style={{width:12,height:12,borderRadius:2,flexShrink:0}} alt="" onError={e=>e.target.style.display="none"}/>
              <span>{lt.length>45?lt.slice(0,45)+"...":lt}</span>
            </a>
            <button onClick={()=>openPreview(lt,lu)} title="Preview" style={{padding:"4px 7px",background:`${t.acc}12`,border:`1px solid ${t.acc}33`,borderRadius:"0 8px 8px 0",borderLeft:"none",color:t.acc,cursor:"pointer",fontSize:11,display:"flex",alignItems:"center"}}>👁</button>
          </span>;
        }
        return <a key={k} href={lu} target="_blank" rel="noopener" style={{color:t.acc,textDecoration:"none",borderBottom:`1px solid ${t.acc}40`}}>{lt}</a>;
      }
      // Bare URL — render as clickable link with favicon
      if(s.match(/^https?:\/\//)){
        const lu=s.replace(/[.,;:!?)]+$/,"");const trail=s.slice(lu.length);
        const qsSrc=_quickSourceForUrl(lu,opts.quickSearch?.results);
        if(qsSrc)return <span key={k}><QuickSourceInlineChip href={lu} label={_qsHost(lu)||lu} source={qsSrc} t={t} font={font} onPreview={openPreview}/>{trail}</span>;
        const hn=(()=>{try{return new URL(lu).hostname.replace("www.","");}catch{return "";}})();
        const display=lu.length>60?lu.slice(0,57)+"...":lu;
        return <span key={k}><a href={lu} target="_blank" rel="noopener" style={{display:"inline-flex",alignItems:"center",gap:4,padding:"2px 8px",background:`${t.warm}10`,border:`1px solid ${t.warm}28`,borderRadius:6,color:t.warm,textDecoration:"none",fontSize:11,maxWidth:340,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",verticalAlign:"middle"}}>
          <img src={`https://www.google.com/s2/favicons?domain=${hn}&sz=16`} style={{width:11,height:11,borderRadius:2,flexShrink:0}} alt="" onError={e=>e.target.style.display="none"}/>
          <span>{display}</span>
        </a>{trail}</span>;
      }
      // Inline verbatim quote: "text 10+ chars" — highlight as word-for-word source
      if(s.startsWith('"')&&s.endsWith('"')&&s.length>11)
        return <span key={k} style={{color:t.warm,background:`${t.warm}15`,padding:"1px 4px",borderRadius:3,fontStyle:"italic",borderBottom:`1px dashed ${t.warm}66`}} title="Verbatim quote">{s}</span>;
      // Keyboard keys: <kbd>Ctrl</kbd>
      if(s.startsWith("<kbd>")&&s.endsWith("</kbd>")){
        const kv=s.slice(5,-6);
        return <kbd key={k} style={{display:"inline-block",padding:"1px 6px",background:`linear-gradient(180deg,${t.surface}EE,${t.bgDeep})`,border:`1px solid ${t.brd}AA`,borderBottomWidth:2,borderRadius:4,fontSize:"0.82em",fontFamily:font,color:t.text,boxShadow:`0 1px 0 ${t.brd}88`,verticalAlign:"baseline",margin:"0 1px",minWidth:14,textAlign:"center",lineHeight:1.4}}>{kv}</kbd>;
      }
      // Inline color swatch: #rrggbb, #rrggbbaa, rgb(), rgba(), hsl(), hsla()
      if(s.match(/^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/)||s.match(/^(rgba?|hsla?)\(/)){
        return <InlineColorSwatch key={k} value={s} theme={t} font={font} printMode={printMode}/>;
      }
      return s;
    });
  };

  // Streaming markdown sanitizer — closes unclosed fences/backticks before rendering
  const mdStream=(text,opts={})=>{
    if(!text)return null;
    let t2=text;
    // Only line-start ``` (allowing up to 3 spaces of CommonMark indent) counts as a fence delimiter.
    const fences=(t2.match(/(?:^|\n)[ \t]{0,3}```/g)||[]).length;
    if(fences%2!==0)t2+="\n```";
    // Count single backticks (exclude triple backticks first)
    const stripped=t2.replace(/```/g,"");
    const ticks=(stripped.match(/`/g)||[]).length;
    if(ticks%2!==0)t2+="`";
    return md(t2,{...opts,streaming:true});
  };

  const md=(text,opts={})=>{
    if(!text)return null;
    const printMode=!!opts.printMode;
    const streaming=!!opts.streaming;
    const rt=opts.theme||t;
    const ip=v=>inlineParse(v,opts);
    // Strip <think>...</think> blocks (model reasoning not for display)
    text = text.replace(/<think>[\s\S]*?<\/think>/g,'').trimStart();
    if(!text)return null;
    text = normalizeRenderableFences(text);
    // Normalize bare /api/downloads/ paths to markdown download links
    text = text.replace(/(?<!\]\()\/api\/downloads\/([^\s)"'`\]]+)/g, '[📎 $1](/api/downloads/$1)');
    // Footnotes: extract [^label]: content defs (must start at line), collect, and replace [^label] refs with indexed placeholders
    const _fnDefs={}; const _fnOrder=[]; const _fnIdx={};
    text = text.replace(/^\[\^([^\]]+)\]:\s*(.+)$/gm,(_m,lbl,body)=>{_fnDefs[lbl]=body.trim();return "";});
    const _fnPrefix=Math.random().toString(36).slice(2,8);
    text = text.replace(/\[\^([^\]]+)\]/g,(m,lbl)=>{
      if(!(lbl in _fnIdx)){_fnOrder.push(lbl);_fnIdx[lbl]=_fnOrder.length;}
      return ""+_fnPrefix+":"+_fnIdx[lbl]+"";
    });
    // KB citations: numbered [n] markers from RAG excerpts → CitationChip placeholders
    // (same private-use masking as footnotes; "cite" can't collide with the 6-char
    // random footnote prefix). Lookbehind keeps `arr[1]`-style indexing intact.
    if(opts.citations?.length){
      const _nc=opts.citations.length;
      text = text.replace(/(?<![\w`\]])\[(\d{1,2})\](?!\()/g,(m,nstr)=>{
        const cn=parseInt(nstr,10);
        return (cn>=1&&cn<=_nc)?"cite:"+cn+"":m;
      });
    }
    // Auto-close unclosed fences so a model that opens ```chart and gets interrupted
    // by a tool call (or just forgets) still renders as a block instead of raw text.
    // Count only line-start ``` (allowing up to 3 spaces of CommonMark indent) so inline
    // mentions like ` ```chart ` in prose don't throw off the balance.
    if(((text.match(/(?:^|\n)[ \t]{0,3}```/g)||[]).length)%2!==0)text+="\n```";
    const _rendered = text.split(/((?<=^|\n)[ \t]{0,3}```[\s\S]*?\n[ \t]{0,3}```[ \t]*(?=\n|$)|<details[\s\S]*?<\/details>)/g).map((p,i)=>{
      if(p.startsWith("<details")){
        const sm=p.match(/<summary[^>]*>([\s\S]*?)<\/summary>/i);
        const summary=sm?sm[1].trim():"Details";
        const body=p
          .replace(/^<details[^>]*>/i,"")
          .replace(/<\/details>\s*$/i,"")
          .replace(/<summary[^>]*>[\s\S]*?<\/summary>/i,"")
          .trim();
        const open=/^<details[^>]*\bopen\b/i.test(p);
        if(printMode)return <div key={i} className="print-details"><div className="print-details-summary">{summary}</div>{md(body,opts)}</div>;
        return <Collapsible key={i} theme={t} font={font} summary={summary} defaultOpen={open} variant={opts.changelog?"changelog":""}>{md(body,opts)}</Collapsible>;
      }
      if(/^[ \t]{0,3}```/.test(p)){
        const m=p.match(/^[ \t]*```(\w*)\n?([\s\S]*?)\n?[ \t]*```[ \t]*$/);
        const lang=m?.[1]||"";
        let code=m?.[2]||p.replace(/^[ \t]*```[^\n]*\n?/,"").replace(/\n?[ \t]*```[ \t]*$/,"");
        // Dedent: strip common leading whitespace (LLMs often indent fences inside list items)
        const _codeLines=code.split("\n");
        const _indents=_codeLines.filter(l=>l.trim()).map(l=>(l.match(/^[ \t]*/)?.[0].length)||0);
        const _minIndent=_indents.length?Math.min(..._indents):0;
        if(_minIndent>0)code=_codeLines.map(l=>l.slice(_minIndent)).join("\n");
        const bid=`c${i}`;
        if(lang==="mermaid")return <MermaidBlock key={i} code={code} theme={rt} font={font} epoch={mermaidEpoch} printMode={printMode} streaming={streaming}/>;
        if(lang==="chart"||lang==="pygraph")return <ChartBlock key={i} code={code} theme={rt} font={font} epoch={mermaidEpoch} kind={lang} printMode={printMode} streaming={streaming}/>;
        if((lang==="json"||!lang)&&looksLikeChartConfig(code))return <ChartBlock key={i} code={code} theme={rt} font={font} epoch={mermaidEpoch} kind="chart" printMode={printMode} streaming={streaming}/>;
        const isDiff=lang==="diff";
        if(printMode)return <pre key={i} className="print-code-block"><code>{code}</code></pre>;
        return <div key={i} style={{margin:"10px 0",borderRadius:10,overflow:"hidden",border:`1px solid ${t.brd}55`,background:t.bgDeep}}>
          <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",padding:"5px 12px",background:`${t.surface}BB`,borderBottom:`1px solid ${t.brd}33`}}>
            <span style={{display:"flex",alignItems:"center",gap:5,fontSize:10,color:t.mut,textTransform:"uppercase",letterSpacing:1}}><IC.Terminal/>{lang||"code"}</span>
            <button onClick={()=>cp(code,bid)} style={{background:"none",border:"none",color:copied===bid?t.ok:t.dim,cursor:"pointer",padding:3,display:"flex",alignItems:"center",gap:4,fontSize:10,fontFamily:font}}>
              {copied===bid?<><IC.Check/> copied</>:<><IC.Copy/> copy</>}
            </button>
          </div>
          {isDiff
            ?<pre style={{margin:0,padding:"8px 0",fontSize:12,lineHeight:1.6,overflowX:"auto",fontFamily:font,color:t.dim}}>{code.split("\n").map((ln,li)=>{
                const c0=ln.charAt(0);
                const isAdd=c0==="+"&&!ln.startsWith("+++");
                const isDel=c0==="-"&&!ln.startsWith("---");
                const isHunk=ln.startsWith("@@");
                const isMeta=ln.startsWith("+++")||ln.startsWith("---")||ln.startsWith("diff ")||ln.startsWith("index ");
                const bg=isAdd?`${t.ok}12`:isDel?`${t.err}12`:isHunk?`${t.acc}10`:"transparent";
                const col=isAdd?t.ok:isDel?t.err:isHunk?t.acc:isMeta?t.mut:t.dim;
                const fs=isMeta?"italic":"normal";
                return <div key={li} style={{padding:"0 12px",background:bg,color:col,fontStyle:fs,whiteSpace:"pre"}}>{ln||" "}</div>;
              })}</pre>
            :<CodeBlock code={code} lang={lang} t={t} font={font}/>}
        </div>;
      }
      // Protect $$/$ sequences inside single-line backtick inline code from the display-math split below
      const _DD="",_SD="";
      const pMasked=p.replace(/`[^`\n]+`/g,m=>m.replace(/\$\$/g,_DD).replace(/\$/g,_SD));
      return <div key={i}>{pMasked.split(/(\$\$[\s\S]+?\$\$)/g).map((seg,si)=>{
        if(seg.startsWith("$$")&&seg.endsWith("$$")&&seg.length>=4){
          return <div key={`m${si}`} style={{margin:"12px 0",textAlign:"center",overflowX:"auto",color:rt.text,fontSize:"1.05em"}}>{seg}</div>;
        }
        seg=seg.split(_DD).join("$$").split(_SD).join("$");
        const _lines=seg.split("\n");
        const _consumed=new Set();
        return <React.Fragment key={`s${si}`}>{_lines.map((ln,j)=>{
        if(_consumed.has(j))return null;
        if(ln.match(/^---+$|^\*\*\*+$|^___+$/))return <hr key={j} style={{border:"none",borderTop:`1px solid ${rt.brd}44`,margin:"10px 0"}}/>;
        if(ln.match(/^##### /))return <div key={j} style={{fontSize:12,fontWeight:700,color:rt.acc,margin:"8px 0 3px"}}>{ip(ln.slice(6))}</div>;
        if(ln.match(/^#### /))return <div key={j} style={{fontSize:13,fontWeight:700,color:rt.acc,margin:"10px 0 4px"}}>{ip(ln.slice(5))}</div>;
        if(ln.match(/^### /))return <div key={j} style={{fontSize:14,fontWeight:700,color:rt.acc,margin:"12px 0 5px"}}>{ip(ln.slice(4))}</div>;
        if(ln.match(/^## /))return <div key={j} style={{fontSize:15,fontWeight:700,color:rt.acc,margin:"14px 0 6px"}}>{ip(ln.slice(3))}</div>;
        if(ln.match(/^# /))return <div key={j} style={{fontSize:17,fontWeight:700,color:rt.acc,margin:"16px 0 7px"}}>{ip(ln.slice(2))}</div>;
        /* GitHub-style callout: > [!NOTE|TIP|IMPORTANT|WARNING|CAUTION] */
        const mCallout=ln.match(/^>\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*(.*)$/i);
        if(mCallout){
          const ctype=mCallout[1].toUpperCase();
          const firstRest=mCallout[2]||"";
          const bodyLines=[];
          if(firstRest.trim())bodyLines.push(firstRest);
          let ck=j+1;
          while(ck<_lines.length && _lines[ck].match(/^>\s?/)){
            bodyLines.push(_lines[ck].replace(/^>\s?/,""));
            _consumed.add(ck);
            ck++;
          }
          const cfg={
            NOTE:{color:rt.acc,icon:"i",label:"Note"},
            TIP:{color:rt.ok,icon:"tip",label:"Tip"},
            IMPORTANT:{color:rt.warm,icon:"*",label:"Important"},
            WARNING:{color:"#f0a030",icon:"⚠",label:"Warning"},
            CAUTION:{color:rt.err,icon:"!",label:"Caution"}
          }[ctype];
          return <div key={j} style={{borderLeft:`3px solid ${cfg.color}`,background:`${cfg.color}12`,padding:"8px 12px 10px",borderRadius:"0 6px 6px 0",margin:"8px 0"}}>
            <div style={{display:"flex",alignItems:"center",gap:6,marginBottom:bodyLines.length?5:0,fontSize:10,fontWeight:700,color:cfg.color,textTransform:"uppercase",letterSpacing:0.8}}>
              <span style={{fontSize:13,lineHeight:1}}>{cfg.icon}</span>
              <span>{cfg.label}</span>
            </div>
            {bodyLines.length>0 && <div style={{color:rt.text,fontSize:"0.95em",lineHeight:1.55}}>{bodyLines.map((bl,bi)=><div key={bi} style={{minHeight:bl===""?6:"auto"}}>{ip(bl)}</div>)}</div>}
          </div>;
        }
        if(ln.match(/^>\s?/) && !ln.match(/^>\s?[—–]\s/)){
          const qLines=[ln.replace(/^>\s?/,"")];
          let attrLabel=null;
          let qk=j+1;
          while(qk<_lines.length && _lines[qk].match(/^>\s?/)){
            const next=_lines[qk].replace(/^>\s?/,"");
            const am=next.match(/^[—–]\s*(.+)/);
            if(am){attrLabel=am[1];_consumed.add(qk);qk++;break;}
            qLines.push(next);
            _consumed.add(qk);
            qk++;
          }
          while(qLines.length && qLines[qLines.length-1].trim()==="")qLines.pop();
          while(qLines.length && qLines[0].trim()==="")qLines.shift();
          if(!qLines.length)return null;
          return <div key={j} style={{borderLeft:`3px solid ${rt.warm}`,margin:"6px 0",background:`${rt.warm}0D`,borderRadius:"0 6px 6px 0",padding:"8px 12px"}}>
            <div style={{display:"flex",alignItems:"flex-start",gap:6}}>
              <span style={{color:rt.warm,fontSize:16,lineHeight:1,flexShrink:0,marginTop:1}}>"</span>
              <div style={{color:rt.text,fontStyle:"italic",fontSize:"0.95em",lineHeight:1.6,flex:1}}>
                {qLines.map((ql,qi)=><div key={qi} style={{minHeight:ql.trim()===""?6:"auto"}}>{ip(ql)}</div>)}
              </div>
              <span style={{color:rt.warm,fontSize:16,lineHeight:1,flexShrink:0,alignSelf:"flex-end"}}>"</span>
            </div>
            {attrLabel && <div style={{marginTop:4,fontSize:9,color:rt.warm,opacity:0.7,textTransform:"uppercase",letterSpacing:1}}>{ip(attrLabel)}</div>}
          </div>;
        }
        if(ln.match(/^>\s?[—–]\s/))return null;
        if(ln.match(/^[-*+]\s+/)){
          const rest=ln.replace(/^[-*+]\s+/,"");
          const taskMatch=rest.match(/^\[([ xX])\]\s+(.*)$/);
          if(taskMatch){
            const checked=taskMatch[1].toLowerCase()==="x";
            return <div key={j} style={{display:"flex",gap:7,alignItems:"flex-start",paddingLeft:4,margin:"1px 0"}}><input type="checkbox" checked={checked} readOnly style={{marginTop:"0.3em",accentColor:rt.acc,flexShrink:0,cursor:"default",width:12,height:12}}/><span style={{flex:1,opacity:checked?0.65:1,textDecoration:checked?"line-through":"none",textDecorationColor:`${rt.mut}88`}}>{ip(taskMatch[2])}</span></div>;
          }
          return <div key={j} style={{display:"flex",gap:7,alignItems:"flex-start",paddingLeft:4,margin:"1px 0"}}><span style={{color:rt.acc,marginTop:"0.25em",fontSize:"0.7em",flexShrink:0}}>●</span><span style={{flex:1}}>{ip(rest)}</span></div>;
        }
        if(ln.match(/^\d+\.\s+/)){const num=ln.match(/^(\d+)\./)[1];return <div key={j} style={{display:"flex",gap:7,alignItems:"flex-start",paddingLeft:4,margin:"1px 0"}}><span style={{color:rt.acc,minWidth:18,flexShrink:0,fontWeight:600,fontSize:"0.9em"}}>{num}.</span><span style={{flex:1}}>{ip(ln.replace(/^\d+\.\s+/,""))}</span></div>;}
        if(ln.match(/^\|.+\|/)){
          /* Group contiguous pipe-rows into one <table> with header + alignment */
          const tRows=[ln];
          let tk=j+1;
          while(tk<_lines.length && _lines[tk].match(/^\|.+\|/)){
            tRows.push(_lines[tk]);_consumed.add(tk);tk++;
          }
          const parseRow=r=>r.split("|").slice(1,-1).map(c=>c.trim());
          const isDivRow=r=>r.replace(/[\s\-:|]/g,"").length===0&&r.includes("-");
          let header=null,aligns=[],body=[];
          if(tRows.length>=2&&isDivRow(tRows[1])){
            header=parseRow(tRows[0]);
            aligns=parseRow(tRows[1]).map(c=>{const lf=c.startsWith(":"),rg=c.endsWith(":");return lf&&rg?"center":rg?"right":"left";});
            body=tRows.slice(2).map(parseRow);
          }else{
            body=tRows.filter(r=>!isDivRow(r)).map(parseRow);
          }
          if(printMode)return <table key={j} className="print-md-table">
            {header&&<thead><tr>{header.map((h,hi)=><th key={hi} style={{textAlign:aligns[hi]||"left"}}>{ip(h)}</th>)}</tr></thead>}
            <tbody>{body.map((row,ri)=><tr key={ri}>{row.map((c,ci)=><td key={ci} style={{textAlign:aligns[ci]||"left"}}>{ip(c)}</td>)}</tr>)}</tbody>
          </table>;
          return <div key={j} style={{margin:"10px 0",overflowX:"auto",borderRadius:8,border:`1px solid ${t.brd}44`,background:`${t.bgDeep}66`}}>
            <table style={{borderCollapse:"collapse",width:"100%",fontSize:12,fontFamily:font}}>
              {header&&<thead><tr>{header.map((h,hi)=><th key={hi} style={{textAlign:aligns[hi]||"left",padding:"7px 12px",borderBottom:`1px solid ${t.brd}66`,background:`${t.surface}AA`,color:t.acc,fontWeight:700,fontSize:10,letterSpacing:0.5,textTransform:"uppercase",whiteSpace:"nowrap"}}>{ip(h)}</th>)}</tr></thead>}
              <tbody>{body.map((row,ri)=><tr key={ri} style={{borderBottom:ri<body.length-1?`1px solid ${t.brd}22`:"none"}}>{row.map((c,ci)=><td key={ci} style={{textAlign:aligns[ci]||"left",padding:"6px 12px",color:t.dim,verticalAlign:"top",lineHeight:1.5,wordBreak:"break-word"}}>{ip(c)}</td>)}</tr>)}</tbody>
            </table>
          </div>;
        }
        return <div key={j} style={{minHeight:ln===""?8:"auto"}}>{ip(ln)}</div>;
      })}</React.Fragment>;
      })}</div>;
    });
    if(_fnOrder.length>0){
      return <React.Fragment>{_rendered}<div style={{marginTop:14,paddingTop:10,borderTop:`1px solid ${rt.brd}33`,fontSize:11,color:rt.mut}}>
        <div style={{fontSize:9,fontWeight:700,letterSpacing:1,textTransform:"uppercase",color:rt.mut,marginBottom:6,opacity:.7}}>Footnotes</div>
        {_fnOrder.map((lbl,ix)=>{const num=ix+1;return <div key={lbl} id={`fn-${_fnPrefix}-${num}`} style={{display:"flex",gap:8,margin:"2px 0",lineHeight:1.5}}>
          <span style={{color:rt.acc,fontWeight:700,flexShrink:0,minWidth:16}}>{num}.</span>
          <span style={{flex:1,color:rt.dim}}>{ip(_fnDefs[lbl]||"")}</span>
        </div>;})}
      </div></React.Fragment>;
    }
    return _rendered;
  };

  const glass={background:`${t.surface}F2`,backdropFilter:"none",border:`1px solid ${t.brd}55`,boxShadow:"none"};
  const _navShort={Chat:"Chat",Artifacts:"Artifacts",Memory:"Memory","Knowledge Bases":"KB",Tools:"Tools",Agents:"Agents","Prompt Library":"Prompts","Deep Research":"Research","Council of AI":"Council","Image Studio":"Image Gen","Model Manager":"Models",Analytics:"Stats",Settings:"Settings",More:"More"};
  const openSettings=()=>{setShowNavMore(false);if(panel!=="settings")previousPanelRef.current=panel;setPanel("settings");};
  const closeSettings=()=>{setShowHealthMonitor(false);setPanel(previousPanelRef.current||"chat");};
  const navBtnS=(active=false,extra={})=>({
    width:52,height:showNavLabels?48:46,display:"flex",flexDirection:"column",alignItems:"center",justifyContent:"center",gap:0,borderRadius:8,cursor:"pointer",
    border:`1px solid ${active?`${t.acc}55`:"transparent"}`,background:active?`${t.acc}14`:"transparent",color:active?t.acc:t.mut,position:"relative",transition:"all .15s",flexShrink:0,boxShadow:"none",padding:showNavLabels?"3px 0 1px":0,
    "--nav-hover-bg":`${t.acc}12`,"--nav-hover-border":`${t.acc}42`,"--nav-hover-color":t.acc,"--nav-hover-ring":`${t.acc}18`,"--nav-hover-shadow":`${t.acc}12`,"--nav-active-hover-bg":`${t.acc}1C`,
    ...extra
  });
  const nb=(p,ico,lab)=>{const active=panel===p;return <button className={`nav-panel-button${active?" is-active":""}`} onClick={()=>setPanel(p)} title={lab} style={navBtnS(active)}><span style={{fontSize:showNavLabels?15:18,display:"flex",alignItems:"center",justifyContent:"center"}}>{ico}</span>{showNavLabels&&<div style={{fontSize:8,marginTop:2,lineHeight:1,opacity:.78,textAlign:"center"}}>{_navShort[lab]||lab}</div>}{active&&<div style={{position:"absolute",left:-1,top:"20%",width:3,height:"60%",borderRadius:"0 2px 2px 0",background:t.warm}}/>}</button>;};
  const moreNavItems=[
    ["artifacts",<IC.Layers/>,"Artifacts",t.acc],
    ["memory",<IC.Brain/>,"Memory",t.f4],
    ["prompts",<IC.Zap/>,"Prompt Library",t.warm],
    ["kb",<IC.Database/>,"Knowledge Bases",t.ok],
    ["tools",<IC.Tool/>,"Tools",t.warm],
    ["models",<IC.Layers/>,"Model Manager",t.f1],
    ["analytics",<IC.BarChart/>,"Analytics",t.mut],
  ];
  const moreNavActive=moreNavItems.some(([p])=>panel===p);
  const svc=n=>{const s=health[n];const c=s?.status==="ok"?t.ok:s?.status==="degraded"?"#f0a030":t.err;const rl=s?.rate_limited?" [Rate Limited]":"";return <div title={`${n}: ${s?.status||"?"}${rl}${s?.response_ms!=null?" ("+s.response_ms+"ms)":""}`} style={{width:6,height:6,borderRadius:"50%",background:c,boxShadow:`0 0 6px ${c}88`}}/>;};
  const filtC=convs.filter(c=>{
    if(isGhostConv(c))return false;
    const matchSearch=(c.title||"").toLowerCase().includes(sq.toLowerCase());
    const tags=convTags[c.id]||[];
    const matchTag=!activeTagFilter||tags.includes(activeTagFilter);
    return matchSearch&&matchTag;
  });
  const inputS={width:"100%",background:`${t.bgDeep}E6`,border:`1px solid ${t.brd}55`,color:t.text,padding:"9px 12px",borderRadius:7,fontFamily:font,fontSize:13,outline:"none",boxSizing:"border-box"};
  const labelS={fontSize:11,color:t.mut,display:"block",marginBottom:5,textTransform:"uppercase",letterSpacing:.6};
  const cardS={background:`${t.surface}F0`,border:`1px solid ${t.brd}55`,borderRadius:8,padding:18,marginBottom:14,animation:"fadeIn .3s",boxShadow:"none"};
  const settingsCardS={background:`${t.surface}40`,border:`1px solid ${t.brd}24`,borderRadius:12,padding:"16px 18px",marginBottom:14};
  const settingsKickerS={fontSize:10,color:t.mut,lineHeight:1.45};
  const btnS=(c,bg)=>({background:bg||`${c}18`,border:`1px solid ${c}4D`,color:c,padding:"6px 12px",borderRadius:7,cursor:"pointer",fontFamily:font,fontSize:11,display:"flex",alignItems:"center",gap:5,boxShadow:"none"});
  const msgToolbarS={display:"inline-flex",alignItems:"center",gap:1,marginTop:5,background:`${t.surface}5c`,border:`1px solid ${t.brd}20`,borderRadius:7,padding:2};
  const msgActionS=(c=t.mut)=>({background:"transparent",border:"none",color:c,cursor:"pointer",padding:"3px 7px",borderRadius:5,fontFamily:font,fontSize:9,display:"flex",alignItems:"center",gap:3});
  const isEmptyChatSurface=panel==="chat"&&!loadingConv&&!councilRunning&&!streaming&&(!act||(!(act.messages||[]).length&&!act.is_council));
  const emptyComposerLift=isEmptyChatSurface?"translate3d(0,clamp(-410px,calc(-50vh + 165px),-205px),0)":"translate3d(0,0,0)";
  useEffect(()=>{
    if(!isEmptyChatSurface)return;
    const id=setTimeout(()=>{
      try{inpRef.current?.focus({preventScroll:true});}catch{inpRef.current?.focus();}
    },760);
    return()=>clearTimeout(id);
  },[isEmptyChatSurface]);
  const latestLivePhase=(()=>{
    const useful=evts.filter(e=>["thinking","thought_done","tool_start","tool_progress","tool_status","tool_end","tool_done","tool_error","error","search_results"].includes(e.type));
    for(let i=useful.length-1;i>=0;i--){
      const phase=_phaseFromEvent(useful[i]);
      if(phase)return phase;
    }
    return null;
  })();
  const currentRun=(()=>{
    if(councilRunning)return{phase:councilVoting?"voting":"council",label:councilVoting?"Council voting":"Council running",detail:councilRound?.label||"Members are responding",color:t.pink,pct:null};
    if(searchLoading)return{phase:"searching",label:"Searching",detail:"Quick Search is gathering sources",color:t.f1,pct:null};
    if(streaming){
      const phase=latestLivePhase&&latestLivePhase.phase!=="complete"?latestLivePhase:{phase:"streaming",label:tokC>0?"Streaming":"Starting",detail:tokC>0?`${tokC} tokens${tokS?` at ${tokS} tok/s`:""}`:"Waiting for first token",pct:null};
      const c=phase.phase==="thinking"?t.f4:phase.phase==="tool"?t.acc:phase.phase==="failed"?t.err:t.warm;
      return{...phase,color:c};
    }
    if(currentRunNotice)return{phase:currentRunNotice.type,label:currentRunNotice.label,detail:currentRunNotice.detail,color:currentRunNotice.type==="failed"?t.err:t.mut,pct:null};
    return null;
  })();
  const composerState=streaming||councilRunning?"streaming":(currentRunNotice?.type==="failed"||quickSearchError)?"error":currentRunNotice?.type==="stopped"?"stopped":composerFocused?"focused":"idle";
  const composerActive=streaming||councilRunning;
  const composerColor=composerState==="error"?t.err:composerState==="stopped"?t.mut:councilRunning?t.pink:streaming?t.warm:composerFocused?t.acc:quickSearch?t.f1:t.acc;
  const sidebarSearchActive=showMessageSearch||sq||ftsQuery;
  const researchSelectableModels=researchModelOptions(models,modelDetails);
  const selectedResearchModel=researchDraft.model||researchSelectableModels[0]||models[0]||"";
  const selectedResearchCtx=modelContextLength(modelDetails,selectedResearchModel);
  const selectedResearchIsCloud=isCloudModelName(selectedResearchModel);
  const selectedResearchIsMoe=isMoeModelName(selectedResearchModel,modelDetails);
  const localMoeResearchModels=researchSelectableModels.filter(m=>m&&!isCloudModelName(m)&&isMoeModelName(m,modelDetails));
  const cloudResearchModels=researchSelectableModels.filter(isCloudModelName);
  const deepMoeResearchModel=localMoeResearchModels.find(m=>modelContextLength(modelDetails,m)>=131072);
  const clampResearchCtx=(n,lo=8192,hi=131072)=>Math.max(lo,Math.min(hi,Math.round((Number(n)||lo)/2048)*2048));
  const smallResearchRoleModel=researchSelectableModels.find(m=>m&&!isCloudModelName(m)&&!isMoeModelName(m,modelDetails)&&/qwen3\.5:4b|qwen3:14b|gemma|phi|llama3\.1:8b/i.test(m))||"";
  const applyResearchPreset=kind=>{
    if(kind==="balanced_moe"){
      const target=selectedResearchIsMoe&&!selectedResearchIsCloud?selectedResearchModel:localMoeResearchModels[0];
      if(!target)return;
      const ctx=modelContextLength(modelDetails,target)||40960;
      setResearchDraft(p=>({...p,model:target,planner_model:"",auditor_model:""}));
      setResearchNumCtx(clampResearchCtx(Math.min(Math.max(ctx,40960),65536),40960,65536));
    }else if(kind==="deep_moe"){
      const target=(selectedResearchIsMoe&&!selectedResearchIsCloud&&selectedResearchCtx>=131072)?selectedResearchModel:deepMoeResearchModel;
      if(!target)return;
      const ctx=modelContextLength(modelDetails,target)||131072;
      setResearchDraft(p=>({...p,model:target,planner_model:"",auditor_model:""}));
      setResearchNumCtx(clampResearchCtx(Math.min(ctx,131072),40960,131072));
    }else if(kind==="cloud"){
      const target=selectedResearchIsCloud?selectedResearchModel:cloudResearchModels[0];
      if(!target)return;
      setResearchDraft(p=>({...p,model:target,planner_model:target,auditor_model:target}));
    }else if(kind==="fast_planning"){
      if(!smallResearchRoleModel)return;
      setResearchDraft(p=>({...p,planner_model:smallResearchRoleModel,auditor_model:smallResearchRoleModel}));
    }
  };
  const settingsTabs=[
    ["users","👤","Users","Profiles and sign-in"],
    ["connections","🔌","Connections","Runtime endpoints and status"],
    ["generation","⚙️","Model & Generation","Global model behavior"],
    ["daedalus","🏛️","Daedalus","Coder-agent routing"],
    ["appearance","🎨","Appearance","Theme, font, and density"],
    ["rag","🧠","RAG Pipeline","Indexing and retrieval"],
    ["quotes","💬","Loading Quotes","Custom waiting text"],
    ["cleanup","🗃️","Sandbox & Cleanup","Storage maintenance"],
    ["danger","⚠️","Danger Zone","Destructive actions"],
    ["changelog","📋","Changelog","Release notes and updates"],
  ];
  const activeSettingsTitle=(settingsTabs.find(([id])=>id===settingsTab)||settingsTabs[0])[2];
  const selectedLoginUser=users.find(u=>u.id===loginUserId)||users[0]||null;
  const localModelCount=models.filter(m=>!m.startsWith("hf.co/")&&!isCloudModelName(m)).length;
  const hfModelCount=models.filter(m=>m.startsWith("hf.co/")).length;
  const installedModelCount=localModelCount+hfModelCount;
  const mmPanelS={background:`${t.surface}66`,border:`1px solid ${t.brd}24`,borderRadius:8,boxShadow:"none"};
  const mmPanelStrongS={background:`${t.surface}88`,border:`1px solid ${t.brd}33`,borderRadius:8,boxShadow:"none"};
  const mmKickerS={fontSize:10,color:t.mut,textTransform:"uppercase",letterSpacing:.7,fontWeight:800};
  const mmMetaS={fontSize:10,color:t.mut,lineHeight:1.4};
  const mmChipS=(c=t.acc,bg)=>({fontSize:10,padding:"2px 7px",borderRadius:6,background:bg||`${c}16`,border:`1px solid ${c}35`,color:c,fontWeight:800,lineHeight:1.15,whiteSpace:"nowrap"});
  const mmIconBtnS=(c=t.mut)=>({width:28,height:28,borderRadius:7,border:`1px solid ${c}30`,background:`${c}10`,color:c,cursor:"pointer",display:"inline-flex",alignItems:"center",justifyContent:"center",padding:0,flexShrink:0});
  const mmSectionTitleS={fontSize:12,fontWeight:900,color:t.dim,textTransform:"uppercase",letterSpacing:.7,marginBottom:10};

  // ============================================================
  // PANELS
  // ============================================================

  // ============================================================
  // PANELS (inlined in render to avoid focus loss)
  // ============================================================

  // ============================================================
  // RENDER
  // ============================================================
  const _uiScale=uiFontSize/14;
  return <div style={{height:`${(100/_uiScale).toFixed(3)}vh`,width:`${(100/_uiScale).toFixed(3)}vw`,zoom:_uiScale,display:"flex",fontFamily:font,background:t.bg,color:t.text,overflow:"hidden",position:"relative"}}>
    {bgEffect==="dots"&&<div style={{position:"absolute",inset:0,opacity:.42,zIndex:0,pointerEvents:"none",backgroundImage:`radial-gradient(circle,${t.acc}18 1px,transparent 1.4px)`,backgroundSize:"24px 24px"}}/>}
    <BackgroundCanvas effect={bgEffect} t={t}/>
    {bgEffect==="scanlines"&&<div style={{position:"absolute",inset:0,zIndex:1,pointerEvents:"none",background:`linear-gradient(180deg,transparent 0%,${t.acc}04 50%,transparent 100%)`,backgroundSize:"100% 4px",animation:"scanline 8s linear infinite",opacity:.4}}/>}

    {userGateOpen&&<div style={{position:"fixed",inset:0,zIndex:320,background:"rgba(0,0,0,.72)",display:"flex",alignItems:"center",justifyContent:"center",padding:18,backdropFilter:"blur(6px)"}}>
      <div style={{width:"min(520px,94vw)",background:t.bgDeep,border:`1px solid ${t.brd}55`,borderRadius:14,boxShadow:"0 18px 70px rgba(0,0,0,.55)",padding:20,animation:"fadeIn .18s"}}>
        <div style={{display:"flex",alignItems:"center",gap:10,marginBottom:16}}>
          <span style={{display:"flex",color:t.acc}}><IC.User/></span>
          <div>
            <div style={{fontSize:15,fontWeight:900,color:t.text,letterSpacing:.4}}>Sign In</div>
            <div style={{fontSize:10,color:t.mut,marginTop:2}}>HyprChat profiles keep chats, memory, workspaces, agents, and reports separate.</div>
          </div>
        </div>
        <div style={{display:"grid",gap:10,marginBottom:16}}>
          {users.length>0&&<select value={loginUserId} onChange={e=>{setLoginUserId(e.target.value);setLoginPassword("");setLoginError("");}} style={inputS}>
            {users.map(u=><option key={u.id} value={u.id}>{u.name}{u.password_enabled?" (password)":""}</option>)}
          </select>}
          {selectedLoginUser?.password_enabled&&<input type="password" value={loginPassword} onChange={e=>setLoginPassword(e.target.value)} onKeyDown={e=>{if(e.key==="Enter")loginAsUser();}} placeholder="Password" style={inputS} autoFocus/>}
          {loginError&&<div style={{fontSize:11,color:t.err,background:`${t.err}12`,border:`1px solid ${t.err}24`,borderRadius:7,padding:"7px 9px"}}>{loginError}</div>}
          {users.length>0&&<button onClick={()=>loginAsUser()} disabled={!selectedLoginUser} style={{...btnS(t.acc),justifyContent:"center",fontSize:12,padding:"9px 14px",opacity:selectedLoginUser?1:.45}}>
            <IC.Check/> Sign In
          </button>}
        </div>
        <div style={{borderTop:`1px solid ${t.brd}22`,paddingTop:14}}>
          <div style={{fontSize:11,fontWeight:800,color:t.dim,textTransform:"uppercase",letterSpacing:.6,marginBottom:8}}>New Profile</div>
          <div style={{display:"grid",gridTemplateColumns:"minmax(0,1fr) minmax(0,1fr) auto",gap:8}}>
            <input value={newUserName} onChange={e=>setNewUserName(e.target.value)} onKeyDown={e=>{if(e.key==="Enter"&&newUserName.trim())createManagedUser();}} placeholder="Name" style={inputS}/>
            <input type="password" value={newUserPassword} onChange={e=>setNewUserPassword(e.target.value)} onKeyDown={e=>{if(e.key==="Enter"&&newUserName.trim())createManagedUser();}} placeholder="Optional password" style={inputS}/>
            <button onClick={createManagedUser} disabled={!newUserName.trim()} style={{...btnS(t.ok),opacity:newUserName.trim()?1:.45,whiteSpace:"nowrap",justifyContent:"center"}}><IC.Plus/> Create</button>
          </div>
        </div>
      </div>
    </div>}

    {/* NAV RAIL */}
    <div style={{width:68,minWidth:68,display:"flex",flexDirection:"column",alignItems:"center",...glass,borderRight:`1px solid ${t.brd}55`,zIndex:11,padding:"12px 0",gap:4}}>
      <div style={{flex:1,minHeight:0,width:"100%",display:"flex",flexDirection:"column",alignItems:"center",gap:4,overflowY:"auto",overflowX:"hidden",scrollbarWidth:"none",paddingBottom:4}}>
        <button className={`nav-panel-button${sidebarSearchActive?" is-active":""}`} onClick={()=>{if(showMessageSearch&&sidebar)closeSidebarSearch();else openSidebarSearch("titles");}} title="Search conversations" style={navBtnS(!!sidebarSearchActive)}>
          <span style={{fontSize:showNavLabels?15:18,display:"flex",alignItems:"center",justifyContent:"center"}}><IC.Search/></span>
          {showNavLabels&&<div style={{fontSize:8,marginTop:2,lineHeight:1,opacity:.78,textAlign:"center"}}>Search</div>}
          {sidebarSearchActive&&<div style={{position:"absolute",left:-1,top:"20%",width:3,height:"60%",borderRadius:"0 2px 2px 0",background:t.warm}}/>}
        </button>
        {nb("chat",<IC.Chat/>,"Chat")}
        {nb("research",<IC.Research/>,"Deep Research")}
        {nb("council",<IC.Council/>,"Council of AI")}
        {nb("personas",<IC.Cube/>,"Agents")}
        {nb("images",<IC.Image/>,"Image Studio")}
        <button className={`nav-panel-button${(showNavMore||moreNavActive)?" is-active":""}`} onClick={()=>setShowNavMore(p=>!p)} title="More" aria-expanded={showNavMore} style={navBtnS(showNavMore||moreNavActive)}>
          <span style={{fontSize:showNavLabels?15:18,display:"flex",alignItems:"center",justifyContent:"center"}}><IC.More/></span>
          {showNavLabels&&<div style={{fontSize:8,marginTop:2,lineHeight:1,opacity:.78,textAlign:"center"}}>More</div>}
          {moreNavActive&&<div style={{position:"absolute",left:-1,top:"20%",width:3,height:"60%",borderRadius:"0 2px 2px 0",background:t.warm}}/>}
        </button>
        <div aria-hidden={!showNavMore} style={{width:"100%",display:"flex",flexDirection:"column",alignItems:"center",gap:4,overflow:"hidden",maxHeight:showNavMore?392:0,opacity:showNavMore?1:0,transform:showNavMore?"translateY(0)":"translateY(-6px)",transition:"max-height .22s ease, opacity .16s ease, transform .18s ease",pointerEvents:showNavMore?"auto":"none",flexShrink:0}}>
          {moreNavItems.map(([p,ico,lab])=><React.Fragment key={p}>{nb(p,ico,lab)}</React.Fragment>)}
        </div>
      </div>
      <button className={`nav-panel-button${panel==="settings"?" is-active":""}`} onClick={openSettings} title="Settings" style={navBtnS(panel==="settings")}>
        <span style={{fontSize:showNavLabels?15:18,display:"flex",alignItems:"center",justifyContent:"center"}}><IC.Settings/></span>
        {showNavLabels&&<div style={{fontSize:8,marginTop:2,lineHeight:1,opacity:.78,textAlign:"center"}}>Settings</div>}
        {panel==="settings"&&<div style={{position:"absolute",left:-1,top:"20%",width:3,height:"60%",borderRadius:"0 2px 2px 0",background:t.warm}}/>}
      </button>
      <button className="nav-panel-button" onClick={()=>setSidebar(p=>!p)} title={sidebar?"Hide conversations":"Show conversations"} style={navBtnS(false,{height:48,flexDirection:"row",borderRadius:12,padding:0,marginTop:4})}>
        <IC.Sidebar/>
      </button>
    </div>

    {/* CONVERSATION LIST */}
    <div style={{width:sidebar?304:0,minWidth:sidebar?304:0,transition:"all .3s cubic-bezier(.4,0,.2,1)",display:"flex",flexDirection:"column",borderRight:`1px solid ${t.brd}55`,overflow:"hidden",background:`${t.bgDeep}F2`,backdropFilter:"none",position:"relative",zIndex:10}}>
      <div style={{padding:"12px 10px 6px",flexShrink:0}}>
        <div style={{fontSize:13,fontWeight:800,letterSpacing:1.8,textTransform:"uppercase",color:t.acc,marginBottom:6,paddingLeft:2}}>HyprChat <span style={{fontSize:8,fontWeight:700,letterSpacing:1,color:t.warm,opacity:.95,padding:"1px 5px",background:`${t.warm}14`,border:`1px solid ${t.warm}40`,borderRadius:4}}>ALPHA</span></div>
        <button onClick={()=>{openSettings();setSettingsTab("users");}} style={{width:"100%",display:"flex",alignItems:"center",gap:7,background:`${t.surface}44`,border:`1px solid ${t.brd}22`,color:t.dim,borderRadius:8,padding:"6px 8px",fontFamily:font,fontSize:11,cursor:"pointer",marginBottom:8,textAlign:"left"}}>
          <span style={{display:"flex",color:t.acc}}><IC.User/></span>
          <span style={{minWidth:0,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",fontWeight:700}}>{currentUser?.name||"Choose user"}</span>
          <span style={{marginLeft:"auto",fontSize:9,color:t.mut}}>switch</span>
        </button>
      </div>
      <div style={{padding:"0 8px 6px",display:"flex",flexDirection:"column",gap:4,flexShrink:0}}>
        <div style={{display:"flex",gap:4}}>
          <button onClick={startBlankChat} style={{flex:1,display:"flex",alignItems:"center",justifyContent:"center",gap:5,background:t.warm,border:`1px solid ${t.warm}`,color:"#fff",padding:"10px 12px",borderRadius:8,cursor:"pointer",fontFamily:font,fontSize:13,fontWeight:700,boxShadow:"none"}}><IC.Plus/> New Chat</button>
          <button onClick={()=>{setWsPanel(p=>!p);setActiveWs(null);setWsDetail(null);}} style={{...btnS(wsPanel?t.acc:t.mut),padding:"5px 8px",fontSize:11}} title="Workspaces">🗂</button>
        </div>
        {!wsPanel&&<>
        {showMessageSearch&&<div style={{display:"flex",flexDirection:"column",gap:4,animation:"fadeIn .16s both"}}>
          <div style={{position:"relative"}}>
            <input ref={titleSearchRef} value={sq} onChange={e=>setSq(e.target.value)} onKeyDown={e=>{if(e.key==="Escape"){closeSidebarSearch();}}} placeholder="Filter chat titles..." style={{...inputS,padding:"7px 30px 7px 9px",fontSize:11}}/>
            <span style={{position:"absolute",right:9,top:"50%",transform:"translateY(-50%)",color:t.mut,display:"flex",pointerEvents:"none",opacity:.75}}><IC.Search/></span>
          </div>
          <div style={{position:"relative"}}>
            <input ref={ftsRef} value={ftsQuery} onChange={e=>setFtsQuery(e.target.value)} onKeyDown={e=>{if(e.key==="Escape"){closeSidebarSearch();}}} placeholder="Search all messages..." style={{...inputS,padding:"7px 30px 7px 9px",fontSize:11}}/>
            {ftsLoading?<span style={{position:"absolute",right:9,top:"50%",transform:"translateY(-50%)",fontSize:9,color:t.mut}}>...</span>:<button onClick={closeSidebarSearch} title="Close search" style={{position:"absolute",right:5,top:"50%",transform:"translateY(-50%)",background:"none",border:"none",color:t.mut,cursor:"pointer",fontSize:14,lineHeight:1,padding:"2px 4px"}}>×</button>}
          </div>
        </div>}
        {(()=>{
          const convIds=new Set(convs.map(c=>c.id));const allTags=[...new Set(Object.entries(convTags).filter(([k])=>convIds.has(k)).flatMap(([,v])=>v))];
          if(!allTags.length)return null;
          return <div style={{display:"flex",flexWrap:"wrap",gap:3,padding:"2px 0"}}>
            <button onClick={()=>setActiveTagFilter(null)} style={{fontSize:10,padding:"3px 8px",borderRadius:10,background:!activeTagFilter?`${t.acc}22`:`${t.surface}55`,border:`1px solid ${!activeTagFilter?t.acc:t.brd}33`,color:!activeTagFilter?t.acc:t.mut,cursor:"pointer"}}>All</button>
            {allTags.map(tag=><button key={tag} onClick={()=>setActiveTagFilter(activeTagFilter===tag?null:tag)} style={{fontSize:10,padding:"3px 8px",borderRadius:10,background:activeTagFilter===tag?`${t.warm}22`:`${t.surface}55`,border:`1px solid ${activeTagFilter===tag?t.warm:t.brd}33`,color:activeTagFilter===tag?t.warm:t.mut,cursor:"pointer"}}>{tag}</button>)}
          </div>;
        })()}
      </>}
      </div>
      {!wsPanel?<div style={{flex:1,overflowY:"auto",padding:"2px 5px"}}>
        {ftsResults.length>0?<div>
          <div style={{padding:"4px 6px",fontSize:9,color:t.mut,display:"flex",justifyContent:"space-between",alignItems:"center"}}><span>{ftsResults.length} results</span><button onClick={()=>{setFtsQuery("");setFtsResults([]);}} style={{background:"none",border:"none",color:t.mut,cursor:"pointer",fontSize:9,padding:0}}>Clear</button></div>
          {ftsResults.map((r,i)=><div key={i} onClick={()=>{loadConversation(r.conversation_id);closeSidebarSearch();}} style={{padding:"6px 8px",borderRadius:8,cursor:"pointer",marginBottom:2,background:`${t.surface}44`,border:`1px solid ${t.brd}22`}}>
            <div style={{fontSize:11,fontWeight:600,color:t.acc,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{r.conv_title||"Chat"}</div>
            <div style={{fontSize:10,color:t.mut,marginTop:2,display:"flex",gap:4,alignItems:"center"}}><span style={{fontSize:8,padding:"1px 4px",borderRadius:3,background:`${t.brd}22`,color:t.dim}}>{r.role}</span></div>
            <div style={{fontSize:10,color:t.dim,marginTop:2,lineHeight:1.4,maxHeight:40,overflow:"hidden"}} dangerouslySetInnerHTML={{__html:(r.snippet||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/&lt;(\/?mark)&gt;/g,"<$1>")}}/>
          </div>)}
        </div>:(()=>{const pinnedC=filtC.filter(c=>c.pinned==="1"||c.pinned===1);const unpinnedC=filtC.filter(c=>c.pinned!=="1"&&c.pinned!==1);const renderConv=(c)=>{const isCouncil=c.is_council==="1"||c.is_council===1||c.is_council===true;const hasPersona=!!c.persona_name&&!isCouncil;const profileType=hasPersona?getConversationProfileType(c):null;const convProfile=hasPersona?getProfileForConversation(c):null;const convAvatar=hasPersona?(c.persona_avatar||profileAvatar(convProfile)):null;const tags=convTags[c.id]||[];const isAct=c.id===actId&&panel==="chat";const accentColor=isCouncil?t.pink:profileType==="persona"?t.pink:hasPersona?t.acc:t.acc;const isPinned=c.pinned==="1"||c.pinned===1;const title=c.title||"New Chat";const rowBg=isAct?`${accentColor}14`:(isCouncil||hasPersona)?`${accentColor}0C`:"transparent";const effectiveRowBg=rowBg==="transparent"?`${t.bgDeep}F2`:rowBg;const hoverRowBg=isAct?effectiveRowBg:`${t.sfBri}1f`;const measureTitle=e=>{const row=e.currentTarget;const clip=row.querySelector(".conv-title-clip");const text=row.querySelector(".conv-title-text");if(!clip||!text)return;const overflow=Math.max(0,text.scrollWidth-clip.clientWidth);row.style.setProperty("--conv-title-marquee",`${overflow+18}px`);row.setAttribute("data-title-overflow",overflow>3?"1":"0");};const resetTitle=e=>{e.currentTarget.setAttribute("data-title-overflow","0");};return <div key={c.id} style={{marginBottom:1}}>
        <div className={`conv-row${isAct?" is-act":""}`} data-sticky-actions={isPinned||tags.length?"1":"0"} onClick={()=>loadConversation(c.id)} onMouseEnter={measureTitle} onFocus={measureTitle} onMouseLeave={resetTitle} onBlur={resetTitle} style={{padding:"9px 7px 9px 9px",borderRadius:8,cursor:"pointer",background:rowBg,border:isAct?`1px solid ${accentColor}50`:(isCouncil||hasPersona)?`1px solid ${accentColor}25`:"1px solid transparent",display:"flex",alignItems:"center",gap:0,borderLeft:`3px solid ${isAct?accentColor:(isCouncil||hasPersona)?`${accentColor}66`:"transparent"}`,position:"relative",overflow:"hidden","--conv-row-bg":effectiveRowBg,"--conv-row-hover-bg":hoverRowBg,"--conv-action-bg":effectiveRowBg}}>
          <div style={{overflow:"hidden",whiteSpace:"nowrap",fontSize:13,color:isAct?t.text:t.dim,flex:1,minWidth:0,fontWeight:isAct?500:400,display:"flex",alignItems:"center",gap:6}}>
            {isCouncil&&<span style={{flexShrink:0,color:t.pink,display:"flex",opacity:isAct?1:.7}}><IC.Council/></span>}
            {hasPersona&&(convAvatar?<img src={avatarSrc(convAvatar)} style={{width:isAct?24:16,height:isAct?24:16,borderRadius:isAct?7:5,objectFit:"cover",flexShrink:0,opacity:isAct?1:.85}} alt=""/>:<span style={{flexShrink:0,color:accentColor,display:"flex",alignItems:"center",justifyContent:"center",width:isAct?24:16,height:isAct?24:16,opacity:isAct?1:.75}}>{profileType==="persona"?<IC.User/>:<IC.Bot/>}</span>)}
            {c.forked_from&&<span style={{flexShrink:0,color:t.f1||t.acc,display:"flex",opacity:.6}} title="Forked conversation"><IC.GitBranch/></span>}
            <span className="conv-title-clip" title={title} style={{minWidth:0,flex:1,overflow:"hidden",whiteSpace:"nowrap"}}>
              <span className="conv-title-text">{title}</span>
            </span>
          </div>
          <div className={`conv-actions${isPinned||tags.length?" has-sticky-actions":""}`} style={{display:"flex",gap:1,flexShrink:0,alignItems:"center",position:"absolute",right:4,top:"50%",transform:"translateY(-50%)",paddingLeft:0,pointerEvents:"none"}}>
            {(() => {const aBtn={background:"none",border:"none",cursor:"pointer",padding:4,display:"flex",alignItems:"center",justifyContent:"center",borderRadius:5,lineHeight:0,pointerEvents:"auto"};return <>
            <button className={`conv-act${isPinned?" on":""}`} onClick={e=>{e.stopPropagation();const newP=isPinned?"0":"1";uConv(c.id,{pinned:newP});fetch(`${API}/api/conversations/${c.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({pinned:newP})});}} title={isPinned?"Unpin":"Pin to top"} style={{...aBtn,color:isPinned?t.warm:t.mut}}><IC.Pin/></button>
            <button className={`conv-act${tags.length?" on":""}`} onClick={e=>{e.stopPropagation();setShowTagEditor(showTagEditor===c.id?null:c.id);setNewTagInput("");}} title="Tags" style={{...aBtn,color:tags.length?t.warm:t.mut}}><IC.Tag/></button>
            <button className="conv-act conv-del" onClick={e=>{e.stopPropagation();delChat(c.id);}} title="Delete" style={{...aBtn,color:t.mut}}><IC.Trash/></button>
            </>;})()}
          </div>
        </div>
        {tags.length>0&&<div style={{display:"flex",flexWrap:"wrap",gap:2,padding:"1px 8px 4px"}}>
          {tags.map(tag=><span key={tag} onClick={e=>{e.stopPropagation();setActiveTagFilter(activeTagFilter===tag?null:tag);}} style={{fontSize:8,padding:"1px 5px",borderRadius:8,background:`${t.warm}18`,color:t.warm,border:`1px solid ${t.warm}28`,cursor:"pointer"}}>{tag}</span>)}
        </div>}
        {showTagEditor===c.id&&<div style={{padding:"4px 8px 6px",animation:"fadeIn .15s"}} onClick={e=>e.stopPropagation()}>
          <div style={{display:"flex",gap:4}}>
            <input value={newTagInput} onChange={e=>setNewTagInput(e.target.value)} onKeyDown={e=>{if(e.key==="Enter"&&newTagInput.trim()){const tag=newTagInput.trim();setConvTags(p=>({...p,[c.id]:[...new Set([...(p[c.id]||[]),tag])]}));setNewTagInput("");}if(e.key==="Escape")setShowTagEditor(null);}} placeholder="Add tag..." style={{...inputS,padding:"3px 6px",fontSize:10,flex:1}}/>
            <button onClick={()=>{if(newTagInput.trim()){const tag=newTagInput.trim();setConvTags(p=>({...p,[c.id]:[...new Set([...(p[c.id]||[]),tag])]}));setNewTagInput("");}}} style={{...btnS(t.warm),padding:"3px 7px",fontSize:9}}>+</button>
            <button onClick={()=>setShowTagEditor(null)} style={{...btnS(t.mut),padding:"3px 7px",fontSize:9}}>×</button>
          </div>
          {tags.length>0&&<div style={{display:"flex",flexWrap:"wrap",gap:2,marginTop:3}}>
            {tags.map(tag=><span key={tag} style={{fontSize:10,padding:"2px 6px",borderRadius:8,background:`${t.warm}18`,color:t.warm,border:`1px solid ${t.warm}28`,display:"flex",alignItems:"center",gap:3}}>
              {tag}<span onClick={()=>setConvTags(p=>({...p,[c.id]:(p[c.id]||[]).filter(x=>x!==tag)}))} style={{cursor:"pointer",opacity:.7,fontSize:12,fontWeight:700,lineHeight:1}}>×</span>
            </span>)}
          </div>}
        </div>}
      </div>;};return <>{pinnedC.length>0&&<><div style={{fontSize:9,fontWeight:700,color:t.warm,letterSpacing:.5,textTransform:"uppercase",padding:"4px 8px 2px",opacity:.6}}>{"\uD83D\uDCCC"} Pinned</div>{pinnedC.map(renderConv)}{unpinnedC.length>0&&<div style={{borderTop:`1px solid ${t.brd}22`,margin:"4px 0"}}/>}</>}{unpinnedC.map(renderConv)}</>;})()}
      </div>:<div style={{flex:1,overflowY:"auto",padding:"8px 6px"}}>
        <button onClick={async()=>{
          const r=await fetch(`${API}/api/workspaces`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:"New Workspace"})});
          const d=await r.json();setWorkspaces(p=>[...p,d]);
          setActiveWs(d.id);setWsDetail({...d,conversations:[],files:[],reports:[]});
        }} style={{...btnS(t.acc),width:"100%",marginBottom:8,justifyContent:"center",fontSize:11}}><IC.Plus/> New Workspace</button>
        {!workspaces.length&&<div style={{textAlign:"center",padding:30,color:t.mut,fontSize:11}}>No workspaces yet.<br/>Group related chats and reports together.</div>}
        {workspaces.map(ws=>{
          const topics=Array.isArray(ws.topics)?ws.topics:(typeof ws.topics==="string"?JSON.parse(ws.topics||"[]"):[]);
          const isA=activeWs===ws.id;
          return <div key={ws.id} onClick={async()=>{
            setActiveWs(ws.id);setWsLoading(true);
            const r=await fetch(`${API}/api/workspaces/${ws.id}`);
            setWsDetail(await r.json());setWsLoading(false);
          }} style={{padding:"10px 12px",borderRadius:12,border:`1px solid ${isA?t.acc:t.brd}33`,background:isA?`${t.acc}10`:`${t.surface}44`,marginBottom:6,cursor:"pointer",transition:"all .2s",animation:"fadeIn .3s"}}>
            <div style={{fontWeight:700,fontSize:12,color:isA?t.acc:t.text,marginBottom:3,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{ws.name}</div>
            {ws.description&&<div style={{fontSize:10,color:t.mut,marginBottom:4,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{ws.description}</div>}
            {topics.length>0&&<div style={{display:"flex",flexWrap:"wrap",gap:3,marginBottom:4}}>
              {topics.slice(0,3).map((tp,i)=><span key={i} style={{fontSize:9,padding:"2px 6px",borderRadius:10,background:tp.color+"22",color:tp.color,border:`1px solid ${tp.color}44`,fontWeight:600}}>{tp.label}</span>)}
            </div>}
            <div style={{fontSize:9,color:t.mut,display:"flex",gap:10}}>
              <span>💬 {ws.conv_count||0}</span><span>📊 {ws.report_count||0}</span><span>📎 {ws.file_count||0}</span>
            </div>
          </div>;
        })}
      </div>}
      <div style={{padding:"7px 11px",borderTop:`1px solid ${t.brd}22`,fontSize:10,color:t.mut,display:"flex",alignItems:"center",gap:5,flexWrap:"wrap"}}>
        {svc("ollama")}Ollama {svc("codebox")}CB {svc("searxng")}SX {svc("n8n")}n8n{health.comfyui!==undefined&&<> {svc("comfyui")}SD</>}{health.stt!==undefined&&<> {svc("stt")}STT</>}{health.tts!==undefined&&<> {svc("tts")}TTS</>}
      </div>
    </div>

    {/* MAIN */}
    <div style={{flex:1,display:"flex",flexDirection:"column",zIndex:5,minWidth:0,position:"relative"}}>
      <div style={{padding:"10px 18px",...glass,borderBottom:`1px solid ${t.brd}55`,display:"flex",alignItems:"center",justifyContent:"space-between",gap:8,flexShrink:0,position:"relative",zIndex:100}}>
        <div style={{display:"flex",alignItems:"center",gap:6}}>
          {panel==="chat"&&!act&&<>
            <ModelPicker value={pendingChatModel||models[0]||""} onChange={setPendingChatModel} models={models} modelDetails={modelDetails} t={t} font={font} compact={true} onRefresh={refreshModels}/>
            {pendingPersona&&(()=>{const pendingProfile=getProfileForConversation(pendingPersona);const type=pendingProfile?getProfileType(pendingProfile):(isCoderPersonaName(pendingPersona.persona_name)||(pendingPersona.tool_ids||[]).length?"agent":"persona");const c=type==="persona"?t.pink:t.acc;const label=type==="persona"?"Persona":"Agent";const avatar=pendingPersona.persona_avatar||profileAvatar(pendingProfile);return <><div style={{display:"flex",alignItems:"center",gap:5,padding:"3px 10px",background:`${c}15`,border:`1px solid ${c}28`,borderRadius:20}}>
              {avatar?<img src={avatarSrc(avatar)} style={{width:type==="persona"?24:16,height:type==="persona"?24:16,borderRadius:type==="persona"?7:5,objectFit:"cover"}} alt=""/>:<span style={{display:"flex",color:c,alignItems:"center",justifyContent:"center",width:type==="persona"?24:16,height:type==="persona"?24:16}}>{type==="persona"?<IC.User/>:<IC.Bot/>}</span>}
              <span style={{fontSize:9,fontWeight:800,color:c,textTransform:"uppercase",letterSpacing:.5}}>{label}</span>
              <span style={{fontSize:10,fontWeight:600,color:type==="persona"?t.text:c}}>{pendingPersona.persona_name}</span>
            </div>
            <button onClick={leaveActiveProfile} style={{display:"flex",alignItems:"center",gap:3,padding:"3px 9px",background:`${t.err}12`,border:`1px solid ${t.err}33`,borderRadius:16,cursor:"pointer",fontSize:10,fontWeight:600,color:t.err,fontFamily:font}}>✕ Leave {label}</button></>;})()}
          </>}
          {panel==="chat"&&act&&<>
            {act.forked_from&&<div style={{display:"flex",alignItems:"center",gap:4,padding:"2px 8px",background:`${t.acc}12`,border:`1px solid ${t.acc}28`,borderRadius:16,fontSize:9,color:t.acc,cursor:"pointer"}} onClick={()=>loadConversation(act.forked_from)}>
              <IC.GitBranch/> Forked — click to view original
            </div>}
            {act.is_council?(()=>{
              const cfg=councils.find(c=>c.id===act.council_config_id);
              return <><div style={{display:"flex",alignItems:"center",gap:6,padding:"3px 10px",background:`${t.pink}15`,border:`1px solid ${t.pink}28`,borderRadius:20}}>
                <IC.Council/>
                <span style={{fontSize:10,fontWeight:700,color:t.pink}}>{cfg?.name||"Council"}</span>
                <span style={{fontSize:9,color:t.mut}}>{(cfg?.members||[]).length} members</span>
                {councilRunning&&<div style={{display:"flex",gap:3}}>{[0,1,2].map(i=><div key={i} style={{width:4,height:4,borderRadius:"50%",background:t.pink,animation:`pulse 1.4s ${i*.16}s infinite`}}/>)}</div>}
              </div>
              <button onClick={()=>newChat(true)} style={{display:"flex",alignItems:"center",gap:3,padding:"3px 9px",background:`${t.err}12`,border:`1px solid ${t.err}33`,borderRadius:16,cursor:"pointer",fontSize:10,fontWeight:600,color:t.err,fontFamily:font}}>✕ Leave Council</button></>;
            })():<ModelPicker value={act.model||models[0]||""} onChange={v=>uConv(actId,{model:v})} models={models} modelDetails={modelDetails} t={t} font={font} compact={true} onRefresh={refreshModels}/>}
            {!act.is_council&&act.persona_name&&(()=>{const type=getConversationProfileType(act);const c=type==="persona"?t.pink:t.acc;const label=type==="persona"?"Persona":"Agent";const avatar=act.persona_avatar||profileAvatar(getProfileForConversation(act));return <><div style={{display:"flex",alignItems:"center",gap:5,padding:"3px 10px",background:`${c}15`,border:`1px solid ${c}28`,borderRadius:20}}>
              {avatar?<img src={avatarSrc(avatar)} style={{width:type==="persona"?28:16,height:type==="persona"?28:16,borderRadius:type==="persona"?8:5,objectFit:"cover"}} alt=""/>:<span style={{display:"flex",color:c,alignItems:"center",justifyContent:"center",width:type==="persona"?28:16,height:type==="persona"?28:16}}>{type==="persona"?<IC.User/>:<IC.Bot/>}</span>}
              <span style={{fontSize:9,fontWeight:800,color:c,textTransform:"uppercase",letterSpacing:.5}}>{label}</span>
              <span style={{fontSize:10,fontWeight:600,color:type==="persona"?t.text:c}}>{act.persona_name}</span>
            </div>
            <button onClick={leaveActiveProfile} style={{display:"flex",alignItems:"center",gap:3,padding:"3px 9px",background:`${t.err}12`,border:`1px solid ${t.err}33`,borderRadius:16,cursor:"pointer",fontSize:10,fontWeight:600,color:t.err,fontFamily:font}}>✕ Leave {label}</button></>;})()}
            {!act.is_council&&!act.persona_name&&<div style={{position:"relative"}}>
              <button onClick={()=>setShowSysPromptPicker(p=>!p)} style={{fontSize:10,padding:"3px 8px",borderRadius:8,border:`1px solid ${act.system_prompt?`${t.warm}44`:`${t.brd}33`}`,background:act.system_prompt?`${t.warm}12`:"transparent",color:act.system_prompt?t.warm:t.mut,cursor:"pointer",fontFamily:font,display:"flex",alignItems:"center",gap:3}}>
                {act.system_prompt?"\u{1F4CB} System Prompt":"\u002B System Prompt"}
              </button>
              {showSysPromptPicker&&<div style={{position:"absolute",top:"calc(100% + 6px)",left:0,zIndex:9999,background:t.bgDeep,border:`1px solid ${t.brd}44`,borderRadius:12,boxShadow:`0 4px 24px #0008`,minWidth:260,maxWidth:340,padding:8,animation:"fadeIn .15s"}}>
                {(()=>{const sysPrompts=prompts.filter(p=>p.is_system||(p.category&&p.category.toLowerCase()==="system prompt"));return sysPrompts.length?<><div style={{maxHeight:200,overflowY:"auto",display:"flex",flexDirection:"column",gap:2}}>
                  {sysPrompts.map(p=><div key={p.id} onClick={()=>{uConv(actId,{system_prompt:p.content});if(!isGhostConv(act))fetch(`${API}/api/conversations/${actId}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({system_prompt:p.content})});setShowSysPromptPicker(false);}} style={{padding:"7px 10px",borderRadius:8,cursor:"pointer",background:`${t.surface}66`,border:`1px solid ${t.brd}22`,transition:"background .12s"}} onMouseEnter={e=>e.currentTarget.style.background=`${t.warm}15`} onMouseLeave={e=>e.currentTarget.style.background=`${t.surface}66`}>
                    <div style={{fontSize:11,fontWeight:600,color:t.text}}>{p.title}</div>
                    <div style={{fontSize:9,color:t.mut,marginTop:1,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{p.content.slice(0,80)}...</div>
                  </div>)}
                </div>
                {act.system_prompt&&<div style={{borderTop:`1px solid ${t.brd}22`,marginTop:4,paddingTop:4}}>
                  <button onClick={()=>{uConv(actId,{system_prompt:""});if(!isGhostConv(act))fetch(`${API}/api/conversations/${actId}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({system_prompt:""})});setShowSysPromptPicker(false);}} style={{...btnS(t.err),width:"100%",justifyContent:"center",fontSize:9}}>Clear System Prompt</button>
                </div>}</>
                :<div style={{padding:12,textAlign:"center",color:t.mut,fontSize:11}}>No system prompt templates. Check "Available as System Prompt" on any prompt in the Prompt Library.</div>;})()}
              </div>}
            </div>}
          </>}
        </div>
        {/* Token counter + export — persistent */}
        <div style={{display:"flex",alignItems:"center",gap:8}}>
          {/* Activity Center */}
          {downloads.length>0&&(()=>{
            const active=downloads.filter(d=>!_activityIsTerminal(d.status));
            const failed=downloads.filter(d=>_activityStatusKey(d.status)==="failed");
            const completed=downloads.filter(d=>_activityStatusKey(d.status)==="complete");
            const fmtSpd=b=>{if(b>=1e6)return`${(b/1e6).toFixed(1)} MB/s`;if(b>=1e3)return`${(b/1e3).toFixed(0)} KB/s`;return`${b.toFixed(0)} B/s`;};
            const fmtEta=s=>{if(s>=3600)return`${Math.floor(s/3600)}h${Math.floor((s%3600)/60)}m`;if(s>=60)return`${Math.floor(s/60)}m ${s%60}s`;return`${s}s`;};
            const groupMeta={Chat:{icon:<IC.Chat/>,color:t.acc},Tools:{icon:<IC.Terminal/>,color:t.warm},Models:{icon:<IC.Layers/>,color:t.f1},Knowledge:{icon:<IC.Database/>,color:t.ok},Cleanup:{icon:<IC.Trash/>,color:t.mut}};
            const statusMeta=(status)=>{const k=_activityStatusKey(status);const map={
              queued:{label:"queued",color:t.mut,bg:`${t.mut}12`},
              running:{label:"running",color:t.acc,bg:`${t.acc}12`},
              indexing:{label:"indexing",color:t.f1,bg:`${t.f1}12`},
              complete:{label:"complete",color:t.ok,bg:`${t.ok}10`},
              failed:{label:"failed",color:t.err,bg:`${t.err}12`},
              canceled:{label:"canceled",color:t.mut,bg:`${t.mut}12`},
            };return map[k]||map.queued;};
            const groups=downloads.reduce((acc,d)=>{const k=_activityGroupKey(d);(acc[k]||(acc[k]=[])).push(d);return acc;},{});
            const pillColor=failed.length?t.err:active.length?t.acc:t.mut;
            return <div ref={dlPanelRef} style={{position:"relative"}}>
              <button onClick={()=>setDownloadsExpanded(p=>!p)} title="Activity" style={{display:"flex",alignItems:"center",gap:6,padding:"4px 10px",background:`${pillColor}14`,border:`1px solid ${pillColor}44`,borderRadius:9,color:pillColor,cursor:"pointer",fontSize:10,fontWeight:700,fontFamily:font}}>
                <IC.Activity/>
                <span>{active.length?`${active.length} active`:failed.length?`${failed.length} failed`:"Activity"}</span>
                <span style={{fontSize:8,opacity:.6,display:"inline-block",transform:downloadsExpanded?"rotate(180deg)":"none",transition:"transform .2s"}}>▼</span>
              </button>
              {downloadsExpanded&&<div style={{position:"absolute",top:"calc(100% + 8px)",right:0,zIndex:200,background:t.bgDeep,border:`1px solid ${t.brd}44`,borderRadius:12,boxShadow:`0 10px 34px #0009`,width:"min(410px,calc(100vw - 24px))",padding:12,animation:"fadeIn .2s"}}>
                  <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:10,paddingBottom:8,borderBottom:`1px solid ${t.brd}22`,gap:10}}>
                    <div style={{display:"flex",alignItems:"center",gap:7,minWidth:0}}>
                      <span style={{display:"flex",color:t.acc}}><IC.Activity/></span>
                      <span style={{fontSize:12,fontWeight:800,color:t.text}}>Activity</span>
                      <span style={{fontSize:9,color:t.mut,whiteSpace:"nowrap"}}>{downloads.length} total</span>
                    </div>
                    <button disabled={!completed.length} onClick={()=>{setDownloads(p=>p.filter(d=>_activityStatusKey(d.status)!=="complete"));}} style={{background:"none",border:`1px solid ${t.brd}24`,color:completed.length?t.mut:`${t.mut}66`,cursor:completed.length?"pointer":"default",fontSize:9,padding:"3px 7px",borderRadius:6,fontFamily:font}}>Clear completed</button>
                  </div>
                  <div style={{display:"flex",flexDirection:"column",gap:10,maxHeight:390,overflowY:"auto",paddingRight:2}}>
                    {["Chat","Tools","Models","Knowledge","Cleanup"].filter(g=>groups[g]?.length).map(g=>{
                      const gm=groupMeta[g];
                      return <div key={g} style={{display:"flex",flexDirection:"column",gap:6}}>
                        <div style={{display:"flex",alignItems:"center",gap:6,fontSize:9,fontWeight:800,color:gm.color,textTransform:"uppercase",letterSpacing:.7}}>
                          <span style={{display:"flex"}}>{gm.icon}</span><span>{g}</span><span style={{color:t.mut,fontWeight:600,letterSpacing:0}}>{groups[g].length}</span>
                        </div>
                        {groups[g].map(dl=>{
                          const sm=statusMeta(dl.status);
                          const terminal=_activityIsTerminal(dl.status);
                          const activeRow=!terminal;
                          const pct=Math.max(0,Math.min(100,Number(dl.pct||0)));
                          const elapsed=_fmtElapsed(dl.startTime,activityNow);
                          return <div key={dl.id} style={{background:activeRow?`${gm.color}0d`:`${t.surface}5c`,borderRadius:8,border:`1px solid ${activeRow?gm.color:t.brd}24`,padding:"8px 9px",opacity:(terminal&&sm.label==="complete")?0.72:1}}>
                            <div style={{display:"flex",alignItems:"center",gap:7,minWidth:0}}>
                              <span style={{width:7,height:7,borderRadius:"50%",background:sm.color,boxShadow:activeRow?`0 0 8px ${sm.color}88`:"none",animation:activeRow?"pulse 1.6s infinite":"none",flexShrink:0}}/>
                              <span title={dl.name} style={{fontSize:11,fontWeight:700,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",flex:1}}>{dl.name}</span>
                              <span style={{fontSize:8,padding:"2px 6px",borderRadius:6,background:sm.bg,color:sm.color,fontWeight:800,flexShrink:0,textTransform:"uppercase"}}>{sm.label}</span>
                            </div>
                            {(pct>0||activeRow)&&<div style={{height:3,background:`${t.surface}AA`,borderRadius:4,overflow:"hidden",margin:"7px 0 6px"}}>
                              <div style={{height:"100%",width:`${sm.label==="complete"?100:pct}%`,background:`linear-gradient(90deg,${gm.color},${sm.color})`,borderRadius:4,transition:"width .45s ease"}}/>
                            </div>}
                            <div style={{display:"flex",alignItems:"center",gap:7,minWidth:0}}>
                              <span style={{fontSize:9,color:sm.label==="failed"?t.err:t.mut,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",flex:1}}>{sm.label==="failed"?(dl.error||dl.message||"Failed"):(dl.message||"Working...")}</span>
                              <span title="Started" style={{display:"inline-flex",alignItems:"center",gap:3,fontSize:9,color:t.mut,whiteSpace:"nowrap"}}><IC.Clock/>{dl.startTime?new Date(dl.startTime).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"}):"--"}</span>
                              {elapsed&&<span style={{fontSize:9,color:t.dim,whiteSpace:"nowrap"}}>{elapsed}</span>}
                            </div>
                            <div style={{display:"flex",alignItems:"center",gap:6,marginTop:dl.totalBytes>0||dl.speed>0||dl.retry||dl.chatId||dl.href?6:0,minHeight:dl.retry||dl.chatId||dl.href?20:0}}>
                              {dl.totalBytes>0&&<span style={{fontSize:9,color:t.dim,fontFamily:font}}>{fmtSize(dl.completedBytes||0)}<span style={{opacity:.5}}>/{fmtSize(dl.totalBytes)}</span></span>}
                              {activeRow&&dl.speed>0&&<span style={{fontSize:9,color:t.acc,fontWeight:700,fontFamily:font}}>{fmtSpd(dl.speed)}</span>}
                              {activeRow&&dl.eta>0&&<span style={{fontSize:9,color:t.warm,fontFamily:font}}>~{fmtEta(dl.eta)}</span>}
                              <span style={{flex:1}}/>
                              {dl.retry&&<button onClick={()=>dl.retry()} style={{background:"none",border:`1px solid ${t.acc}35`,color:t.acc,cursor:"pointer",padding:"2px 7px",borderRadius:6,fontFamily:font,fontSize:9,fontWeight:700}}>Retry</button>}
                              {dl.chatId&&<button onClick={()=>{loadConversation(dl.chatId,{force:true});setDownloadsExpanded(false);}} style={{background:"none",border:`1px solid ${t.brd}35`,color:t.mut,cursor:"pointer",padding:"2px 7px",borderRadius:6,fontFamily:font,fontSize:9,fontWeight:700}}>Open</button>}
                              {dl.href&&<a href={dl.href} target="_blank" rel="noreferrer" style={{display:"inline-flex",alignItems:"center",gap:3,textDecoration:"none",border:`1px solid ${t.brd}35`,color:t.mut,padding:"2px 7px",borderRadius:6,fontSize:9,fontWeight:700}}><IC.External/> {dl.actionLabel||"Open"}</a>}
                              <button onClick={()=>setDownloads(p=>p.filter(x=>x.id!==dl.id))} title="Dismiss" style={{background:"none",border:"none",color:t.mut,cursor:"pointer",padding:"1px 3px",display:"flex",opacity:.75}}><IC.X/></button>
                            </div>
                          </div>;
                        })}
                      </div>;
                    })}
                  </div>
                </div>}
            </div>;
          })()}
          {panel==="chat"&&globalMemoryActive&&<span title="HyprChat memory active for this chat" style={{display:"inline-flex",alignItems:"center",gap:6,padding:"4px 9px",borderRadius:8,background:`${t.acc}10`,border:`1px solid ${t.acc}30`,color:t.acc,fontSize:10,fontWeight:800,whiteSpace:"nowrap"}}>
            <span style={{width:6,height:6,borderRadius:"50%",background:t.acc,flexShrink:0}}/>
            Memory
            <span style={{color:t.mut,fontWeight:500}}>HyprChat</span>
          </span>}
          {panel==="chat"&&workspaceMemoryActive&&<span title={`Workspace memory active: ${activeMemoryWs.name}`} style={{display:"inline-flex",alignItems:"center",gap:6,padding:"4px 9px",borderRadius:8,background:`${t.ok}10`,border:`1px solid ${t.ok}30`,color:t.ok,fontSize:10,fontWeight:800,maxWidth:160,overflow:"hidden",whiteSpace:"nowrap"}}>
            <span style={{width:6,height:6,borderRadius:"50%",background:t.ok,flexShrink:0}}/>
            Memory
            <span style={{color:t.mut,fontWeight:500,overflow:"hidden",textOverflow:"ellipsis"}}>{activeMemoryWs.name}</span>
          </span>}
          {panel==="chat"&&ghostActive&&<span title="Ghost mode: this chat stays local and is not saved, indexed, or added to history." style={{display:"inline-flex",alignItems:"center",gap:6,padding:"4px 9px",borderRadius:8,background:`${t.pink}10`,border:`1px solid ${t.pink}30`,color:t.pink,fontSize:10,fontWeight:800,whiteSpace:"nowrap"}}>
            <IC.Ghost/> Ghost
          </span>}
          {panel==="chat"&&<button onClick={toggleGlobalMemory} aria-pressed={globalMemoryActive} disabled={ghostActive} title={act?(globalMemoryActive?"HyprChat memory on: this chat recalls accepted global memories and queues suggestions":"HyprChat memory: recall accepted global memories and queue suggestions for this chat"):(globalMemoryActive?"HyprChat memory on for the next chat":"HyprChat memory: enable recall for the next chat")} style={{background:globalMemoryActive?`${t.acc}18`:"none",border:`1px solid ${globalMemoryActive?t.acc:t.brd}33`,color:ghostActive?`${t.mut}66`:globalMemoryActive?t.acc:t.mut,cursor:ghostActive?"default":"pointer",padding:"4px 8px",borderRadius:8,fontSize:13,display:"flex",alignItems:"center",boxShadow:globalMemoryActive?`0 0 0 1px ${t.acc}22 inset`:"none",opacity:ghostActive?0.45:1}}><IC.Brain/></button>}
          {panel==="chat"&&<button onClick={toggleGhostMode} aria-pressed={ghostActive} title={ghostActive?"Ghost mode on: this chat is local only and will not be saved or indexed":"Ghost mode: start a chat that is never saved or indexed"} style={{background:ghostActive?`${t.pink}18`:"none",border:`1px solid ${ghostActive?t.pink:t.brd}33`,color:ghostActive?t.pink:t.mut,cursor:"pointer",padding:"4px 8px",borderRadius:8,fontSize:13,display:"flex",alignItems:"center",boxShadow:ghostActive?`0 0 0 1px ${t.pink}22 inset`:"none"}}><IC.Ghost/></button>}
          {/* Dark/Light mode toggle */}
          <button onClick={()=>{if(isLightTheme){setTm(prevDarkTheme);}else{setPrevDarkTheme(tm);localStorage.setItem("hc-prev-dark",tm);setTm(LIGHT_PAIRS[tm]||"oneLight");}}} title={isLightTheme?"Switch to dark mode":"Switch to light mode"} style={{background:"none",border:`1px solid ${t.brd}33`,color:t.mut,cursor:"pointer",padding:"4px 8px",borderRadius:8,fontSize:13,display:"flex",alignItems:"center"}}>{isLightTheme?"\u2600\uFE0F":"\uD83C\uDF19"}</button>
          {/* Export / Import */}
          {panel==="chat"&&act&&(act.messages||[]).length>0&&<>
            <div ref={exportMenuRef} style={{position:"relative"}}>
              <button onClick={()=>setShowExportMenu(p=>!p)} title="Export chat" style={{background:showExportMenu?`${t.acc}12`:"none",border:`1px solid ${showExportMenu?t.acc:t.brd}33`,color:showExportMenu?t.acc:t.mut,cursor:"pointer",padding:"4px 8px",borderRadius:8,fontFamily:font,fontSize:10,display:"flex",alignItems:"center",gap:4}}><IC.Upload/> export</button>
              {showExportMenu&&<div style={{position:"absolute",top:"calc(100% + 6px)",right:0,zIndex:250,background:t.bgDeep,border:`1px solid ${t.brd}44`,borderRadius:8,boxShadow:`0 4px 20px #0008`,minWidth:150,padding:5,animation:"fadeIn .15s"}}>
                <button onClick={()=>{setShowExportMenu(false);exportChat();}} style={{width:"100%",display:"flex",alignItems:"center",justifyContent:"space-between",gap:10,background:"transparent",border:"none",color:t.dim,cursor:"pointer",padding:"7px 9px",borderRadius:6,fontFamily:font,fontSize:11,textAlign:"left"}}><span>Markdown</span><span style={{color:t.mut,fontSize:9}}>MD</span></button>
                <button onClick={()=>{setShowExportMenu(false);exportChatJSON();}} style={{width:"100%",display:"flex",alignItems:"center",justifyContent:"space-between",gap:10,background:"transparent",border:"none",color:t.dim,cursor:"pointer",padding:"7px 9px",borderRadius:6,fontFamily:font,fontSize:11,textAlign:"left"}}><span>JSON</span><span style={{color:t.mut,fontSize:9}}>JSON</span></button>
              </div>}
            </div>
          </>}
          <input ref={importRef} type="file" accept=".json" style={{display:"none"}} onChange={e=>{if(e.target.files?.[0])importChatJSON(e.target.files[0]);e.target.value="";}}/>
          <button onClick={()=>importRef.current?.click()} title="Import chat from JSON" style={{background:"none",border:`1px solid ${t.brd}33`,color:t.mut,cursor:"pointer",padding:"4px 8px",borderRadius:8,fontFamily:font,fontSize:10,display:"flex",alignItems:"center",gap:2}}><IC.Download/> import</button>
          {panel==="chat"&&act&&(()=>{
            // Context meter: prompt + generated tokens against the active num_ctx.
            const promptToks=ctxTokens||0;
            const liveGen=streaming&&tokC>0?tokC:0;
            const totalCtxUsed=streaming?(promptToks+liveGen):(promptToks||sessionTokens||0);
            const toks=totalCtxUsed;
            const modelName=act?.model||models[0]||"";
            const mp=modelParams[modelName]||{};
            const maxCtx=mp.num_ctx&&mp.num_ctx!=="default"?parseInt(mp.num_ctx)||0:numCtx||tokenLimit||0;
            const ratio=maxCtx>0?Math.min(1,toks/maxCtx):0;
            const cc=maxCtx>0?(ratio>0.85?t.err:ratio>0.6?t.warm:t.ok):streaming?t.acc:t.warm;
            const fmtK=n=>n>=1000?`${(n/1000).toFixed(1)}k`:String(n);
            return <div style={{display:"flex",flexDirection:"column",overflow:"hidden",borderRadius:10,border:`1px solid ${cc}45`,background:`${cc}12`,minWidth:90,cursor:"default"}} title={`Context used: prompt + generated tokens = ${toks}${maxCtx>0?` / ${maxCtx}`:""}${liveGen>0?` | Live generation: +${liveGen} tokens`:genTokens>0?` | Generated this turn: ${genTokens} tokens`:""}`}>
              <div style={{display:"flex",alignItems:"center",gap:5,padding:"5px 11px"}}>
                <span style={{fontSize:13,display:"inline-block",animation:streaming?"bop 1.5s ease-in-out infinite":"none"}}>🧠</span>
                <span style={{fontSize:13,color:cc,fontWeight:700,fontFamily:font,letterSpacing:.2}}>{fmtK(toks)}</span>
                {maxCtx>0&&<span style={{fontSize:10,color:t.mut,fontFamily:font}}>/{fmtK(maxCtx)}</span>}
                {streaming&&tokS>0&&<span style={{fontSize:9,color:`${cc}99`,marginLeft:2}}>{tokS}t/s</span>}
              </div>
              {maxCtx>0&&<div style={{height:3,background:`${t.surface}88`}}>
                <div style={{height:"100%",background:cc,width:`${ratio*100}%`,transition:"width .3s ease"}}/>
              </div>}
            </div>;
          })()}
        </div>
      </div>

      {/* Status pills - removed from here, now in chat area */}

      {/* Panels + Preview wrapper */}
      <div style={{flex:1,display:"flex",overflow:"hidden"}}>
      <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden",minWidth:0}}>
      {/* Panels */}
      {wsPanel&&activeWs
        ?<WorkspaceDetail ws={wsDetail} wsLoading={wsLoading} t={t} API={API}
            workspaces={workspaces} setWorkspaces={setWorkspaces}
            setActiveWs={setActiveWs} setWsDetail={setWsDetail}
            convs={convs} onOpenConv={id=>{setWsPanel(false);loadConversation(id);}}
            onOpenReport={id=>{setWsPanel(false);setPanel("research");setResearchView("reports");loadResearchReport(id);}}
            onRemoveReport={removeResearchReportFromWorkspace}
            onPreview={openPreview} models={models} wsModel={wsModel}
            notify={notify}
            onPersonaCreated={mc=>{const norm={...mc,parameters:normalizeProfileParams(mc)};setMcs(p=>[...p,norm]);setProfileTab(getProfileType(norm)==="persona"?"personas":"agents");setPanel("personas");setEditMc(mc.id);}}/>
      :panel==="artifacts"?<ArtifactStudioPanel t={t} font={font} workspaces={workspaces} kbs={kbs} onPreview={openPreview} onOpenConv={id=>loadConversation(id)} onUseInChat={att=>{if(att){setAttachments(p=>[...p,att]);setPanel("chat");notify({type:"success",text:"Artifact added to composer",duration:1800});}}} focusId={artifactFocusId} onFocusConsumed={()=>setArtifactFocusId(null)} notify={notify}/>
      :panel==="images"?<ImageStudioPanel t={t} font={font} configured={!!comfyuiUrl} onPreview={openPreview} onUseInChat={att=>{if(att){setAttachments(p=>[...p,att]);setPanel("chat");notify({type:"success",text:"Image added to composer",duration:1800});}}} notify={notify} confirmAction={confirmAction}/>
      :panel==="memory"?<MemoryProfilePanel t={t} API={API} font={font} notify={notify} onOpenConv={id=>loadConversation(id)} models={models} wsModel={wsModel}/>
      :panel==="kb"?<div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
    <div style={{padding:"16px 20px",borderBottom:`1px solid ${t.brd}28`,display:"flex",justifyContent:"space-between",alignItems:"center"}}>
      <div style={{display:"flex",alignItems:"center",gap:8}}><IC.Database/><span style={{fontSize:14,fontWeight:700,letterSpacing:1,textTransform:"uppercase",color:t.acc}}>Knowledge Bases</span></div>
      <button onClick={async()=>{
        const r=await fetch(`${API}/api/knowledge-bases`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:"New KB",description:""})}).catch(()=>null);
        if(r){const kb=await r.json();setKbs(p=>[...p,{...kb,files:[]}]);}else{setKbs(p=>[...p,{id:`kb-${Date.now()}`,name:"New KB",description:"",files:[]}]);}
      }} style={btnS(t.acc)}><IC.Plus/> New KB</button>
    </div>
    <div style={{flex:1,overflowY:"auto",padding:20}}>
      {kbs.map(kb=><div key={kb.id} style={cardS}>
        <div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",marginBottom:10}}>
          <div style={{flex:1}}>
            <input value={kb.name} onChange={e=>{const next={...kb,name:e.target.value};setKbs(p=>p.map(k=>k.id===kb.id?next:k));scheduleKbSave(next);}} onBlur={e=>flushKbSave({...kb,name:e.target.value})} style={{...inputS,fontSize:14,fontWeight:600,background:"transparent",border:"none",padding:"0 0 4px 0",color:t.text}}/>
            <input value={kb.description||""} onChange={e=>{const next={...kb,description:e.target.value};setKbs(p=>p.map(k=>k.id===kb.id?next:k));scheduleKbSave(next);}} onBlur={e=>flushKbSave({...kb,description:e.target.value})} placeholder="Description..." style={{...inputS,background:"transparent",border:"none",padding:0,fontSize:11,color:t.mut}}/>
          </div>
          <button onClick={async()=>{fetch(`${API}/api/knowledge-bases/${kb.id}`,{method:"DELETE"}).catch(()=>{});setKbs(p=>p.filter(k=>k.id!==kb.id));}} style={btnS(t.err)}><IC.Trash/></button>
        </div>
        <div style={{maxHeight:240,overflowY:"auto",marginBottom:10,display:"flex",flexDirection:"column",gap:3}}>
          {(kb.files||[]).map((f,i)=><div key={i} style={{display:"flex",alignItems:"center",gap:8,padding:"5px 10px",background:`${t.surface}44`,borderRadius:8,border:`1px solid ${t.brd}18`,fontSize:11}}>
            <span style={{color:t.f1,flexShrink:0}}>{(()=>{const ext=((f.filename||f.name||"").split(".").pop()||"").toLowerCase();if(ext==="pdf")return"📕";if(["doc","docx"].includes(ext))return"📘";if(["xls","xlsx","csv"].includes(ext))return"📊";if(["json","xml","yaml","yml"].includes(ext))return"🗂";if(["py","js","ts","rs","go","java","c","cpp","h","rb","sh"].includes(ext))return"💻";if(["md"].includes(ext))return"📝";if(["txt","text","log"].includes(ext))return"📝";if(["html","htm"].includes(ext))return"🌐";if(["png","jpg","jpeg","gif","svg","webp"].includes(ext))return"🖼";if(["zip","tar","gz","rar","7z"].includes(ext))return"📦";return"📄";})()}</span>
            <span style={{flex:1,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",fontWeight:500}}>{f.filename||f.name}</span>
            <span style={{fontSize:9,color:t.mut,flexShrink:0}}>{((f.file_size||f.size||0)/1024).toFixed(1)}KB</span>
            {f.id&&<button onClick={async()=>{const fn=(f.filename||f.name||"").toLowerCase();const ext=fn.split(".").pop();if(ext==="pdf"){setKbPreview({filename:f.filename||f.name,rawUrl:userScopedUrl(`/api/knowledge-bases/${kb.id}/files/${f.id}/raw`),isPdf:true,pdfFullView:false,content:null,loading:true});try{const r=await fetch(`${API}/api/knowledge-bases/${kb.id}/files/${f.id}/pdf-text?pages=10`);const d=await r.json();setKbPreview(p=>({...p,content:d.content,total_pages:d.total_pages,previewed_pages:d.previewed_pages,truncated:d.truncated,loading:false}));}catch(e){setKbPreview(p=>({...p,content:"Failed to extract PDF text: "+e.message,loading:false}));}}else if(["png","jpg","jpeg","gif","svg","webp","bmp"].includes(ext)){setKbPreview({filename:f.filename||f.name,rawUrl:userScopedUrl(`/api/knowledge-bases/${kb.id}/files/${f.id}/raw`),isImage:true,loading:false});}else{setKbPreview({filename:f.filename||f.name,content:null,loading:true});try{const r=await fetch(`${API}/api/knowledge-bases/${kb.id}/files/${f.id}/preview`);const d=await r.json();setKbPreview({filename:d.filename,content:d.content,truncated:d.truncated,total_lines:d.total_lines,loading:false});}catch(e){setKbPreview({filename:f.filename||f.name,content:"Failed to load preview: "+e.message,loading:false});}}}} style={{background:`${t.acc}15`,border:`1px solid ${t.acc}33`,color:t.acc,cursor:"pointer",padding:"2px 8px",borderRadius:5,fontSize:9,fontWeight:600,flexShrink:0}}>Preview</button>}
            <button onClick={()=>{if(f.id)fetch(`${API}/api/knowledge-bases/files/${f.id}`,{method:"DELETE"}).catch(()=>{});setKbs(p=>p.map(k=>k.id===kb.id?{...k,files:(k.files||[]).filter((_,j)=>j!==i)}:k));}} style={{background:"none",border:"none",color:t.err,cursor:"pointer",padding:"2px 4px",fontSize:10,opacity:.6,flexShrink:0}} title="Delete file">✕</button>
          </div>)}
          {!(kb.files||[]).length&&<span style={{fontSize:11,color:t.mut,fontStyle:"italic",padding:"4px 0"}}>No files uploaded</span>}
        </div>
        {/* Upload progress bars */}
        {Object.entries(uploadProgress).filter(([k])=>k.startsWith(kb.id+":")).map(([key,info])=>(
          <div key={key} style={{marginBottom:8,padding:"6px 10px",background:`${t.surface||t.bg}`,borderRadius:6,border:`1px solid ${t.brd}22`}}>
            <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",fontSize:10,marginBottom:4}}>
              <span style={{color:t.text,fontWeight:600}}>{info.filename}</span>
              <span style={{color:info.status==="error"?t.err:info.status==="done"?"#4caf50":t.mut,fontWeight:500}}>
                {info.status==="uploading"?`Uploading ${info.progress}%`:info.status==="indexing"?"Indexing...":info.status==="done"?"Complete":info.status==="error"?(info.error||"Failed"):""}
              </span>
            </div>
            <div style={{height:4,borderRadius:2,background:`${t.mut}22`,overflow:"hidden"}}>
              <div style={{height:"100%",borderRadius:2,background:info.status==="error"?t.err:info.status==="done"?"#4caf50":t.f1,width:`${info.status==="done"?100:info.status==="indexing"?90:info.progress||0}%`,transition:"width 0.3s ease"}}/>
            </div>
          </div>
        ))}
        <label style={{display:"flex",alignItems:"center",gap:6,padding:"8px 14px",background:`${t.f1}10`,border:`1px dashed ${t.f1}44`,borderRadius:8,cursor:"pointer",fontSize:11,color:t.f1}}>
          <IC.Upload/> Upload files
          <input type="file" multiple accept=".pdf,.md,.txt,.json,.csv,.docx,.html,.py,.js,.ts,.yaml,.yml" style={{display:"none"}} onChange={e=>{
            const files=[...e.target.files];e.target.value="";if(!files.length)return;
            const kbId=kb.id;
            const uploadOne=f=>{
              const stamp=Date.now();
              const pKey=`${kbId}:${f.name}:${stamp}`;
              const activityId=`kb-upload-${stamp}-${Math.random().toString(36).slice(2,6)}`;
              addActivity({id:activityId,name:`KB upload: ${f.name}`,kind:"upload",message:"Uploading file...",pct:0,totalBytes:f.size||0});
              setUploadProgress(p=>({...p,[pKey]:{filename:f.name,progress:0,status:"uploading"}}));
              const fd=new FormData();fd.append("file",f);
              const xhr=new XMLHttpRequest();
              xhr.upload.onprogress=ev=>{if(ev.lengthComputable){const pct=Math.round(ev.loaded/ev.total*100);setUploadProgress(p=>({...p,[pKey]:{...p[pKey],progress:pct}}));updateActivity(activityId,{status:"uploading",pct:Math.min(85,pct),message:`Uploading ${pct}%`,completedBytes:ev.loaded,totalBytes:ev.total});}};
              xhr.onload=()=>{
                if(xhr.status>=200&&xhr.status<300){
                  let d;try{d=JSON.parse(xhr.responseText);}catch{d={};}
                  setUploadProgress(p=>({...p,[pKey]:{...p[pKey],status:"indexing",progress:100}}));
                  updateActivity(activityId,{status:"indexing",pct:90,message:"Indexing file..."});
                  // Add file to KB state immediately with server-returned id
                  setKbs(p=>p.map(k=>k.id===kbId?{...k,files:[...(k.files||[]),{id:d.id,filename:d.filename||f.name,file_size:d.file_size||f.size}]}:k));
                  // Poll indexing status then auto-dismiss
                  const poll=async()=>{
                    for(let i=0;i<60;i++){
                      await new Promise(r=>setTimeout(r,2000));
                      try{
                        const sr=await fetch(`${API}/api/knowledge-bases/${kbId}/files/${encodeURIComponent(d.filename||f.name)}/status`);
                        const st=await sr.json();
                        if(st.status==="done"){setUploadProgress(p=>({...p,[pKey]:{...p[pKey],status:"done"}}));updateActivity(activityId,{status:"done",pct:100,message:"Indexed"});setTimeout(()=>setUploadProgress(p=>{const n={...p};delete n[pKey];return n;}),3000);return;}
                        if(st.status==="error"){setUploadProgress(p=>({...p,[pKey]:{...p[pKey],status:"error",error:st.error||"indexing failed"}}));updateActivity(activityId,{status:"error",error:st.error||"indexing failed",message:"Indexing failed"});notify({type:"error",text:"KB indexing failed",detail:st.error||f.name});setTimeout(()=>setUploadProgress(p=>{const n={...p};delete n[pKey];return n;}),6000);return;}
                      }catch{}
                    }
                    setUploadProgress(p=>({...p,[pKey]:{...p[pKey],status:"done"}}));updateActivity(activityId,{status:"done",pct:100,message:"Indexed"});setTimeout(()=>setUploadProgress(p=>{const n={...p};delete n[pKey];return n;}),3000);
                  };
                  poll();
                }else{
                  let msg="Upload failed";try{msg=JSON.parse(xhr.responseText).detail||msg;}catch{}
                  setUploadProgress(p=>({...p,[pKey]:{...p[pKey],status:"error",error:msg}}));
                  updateActivity(activityId,{status:"error",error:msg,message:"Upload failed"});
                  notify({type:"error",text:"KB upload failed",detail:msg});
                  setTimeout(()=>setUploadProgress(p=>{const n={...p};delete n[pKey];return n;}),6000);
                }
              };
              xhr.onerror=()=>{
                setUploadProgress(p=>({...p,[pKey]:{...p[pKey],status:"error",error:"Network error"}}));
                updateActivity(activityId,{status:"error",error:"Network error",message:"Upload failed"});
                notify({type:"error",text:"KB upload failed",detail:"Network error"});
                setTimeout(()=>setUploadProgress(p=>{const n={...p};delete n[pKey];return n;}),6000);
              };
              xhr.open("POST",`${API}/api/knowledge-bases/${kbId}/files`);
              const uid=window.__hyprchatUserId||hcStoredUserId();
              const sess=window.__hyprchatSessionToken||hcStoredSession(uid);
              xhr.setRequestHeader("X-HyprChat-User",uid);
              if(sess)xhr.setRequestHeader("X-HyprChat-Session",sess);
              xhr.send(fd);
            };
            // Fire all uploads in parallel
            files.forEach(uploadOne);
          }}/>
        </label>
      </div>)}
      {!kbs.length&&<div style={{textAlign:"center",padding:40,color:t.mut,fontSize:12}}>No knowledge bases yet.</div>}
    </div>
  </div>

      :panel==="tools"?<div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
    <div style={{padding:"16px 20px",borderBottom:`1px solid ${t.brd}28`,display:"flex",justifyContent:"space-between",alignItems:"center"}}>
      <div style={{display:"flex",alignItems:"center",gap:7}}><IC.Tool/><span style={{fontSize:14,fontWeight:700,letterSpacing:1,textTransform:"uppercase",color:t.warm}}>Tools</span></div>
      <div style={{display:"flex",gap:5}}>
        <button onClick={()=>{setPasteMode(p=>!p);setPasteCode("");setPasteToolName("");setPasteToolDesc("");}} style={btnS(pasteMode?t.warm:t.mut)}>
          {pasteMode?"✕ Cancel":"✦ Paste Code"}
        </button>
        <label style={btnS(t.warm)}><IC.Upload/> Upload .py<input type="file" accept=".py" multiple style={{display:"none"}} onChange={async e=>{
          for(const f of e.target.files){const fd=new FormData();fd.append("file",f);
            try{const r=await fetch(`${API}/api/tools/upload`,{method:"POST",body:fd});const d=await r.json();setTools(p=>[...p,d]);}
            catch{const rd=new FileReader();rd.onload=ev=>{setTools(p=>[...p,{id:`t-${Date.now()}`,name:f.name.replace(".py",""),description:`Uploaded: ${f.name}`,filename:f.name,code:ev.target.result}]);};rd.readAsText(f);}
          }e.target.value="";
        }}/></label>
        <button onClick={async()=>{
          const tl={name:"new_tool",description:"",filename:"new_tool.py",code:"# New tool\ndef run(input: str) -> str:\n    return input"};
          try{const r=await fetch(`${API}/api/tools`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(tl)});const d=await r.json();setTools(p=>[...p,d]);setEditTool(d.id);}
          catch{const id=`t-${Date.now()}`;setTools(p=>[...p,{id,...tl}]);setEditTool(id);}
        }} style={btnS(t.acc)}><IC.Plus/> New</button>
      </div>
    </div>
    {pasteMode&&<div style={{padding:16,borderBottom:`1px solid ${t.brd}22`,background:`${t.warm}07`,animation:"fadeIn .2s"}}>
      <div style={{fontSize:11,fontWeight:700,color:t.warm,marginBottom:8,letterSpacing:.5,textTransform:"uppercase"}}>Paste Python Tool</div>
      <textarea value={pasteCode} rows={10} placeholder={"def my_tool(query: str) -> str:\n    \"\"\"Brief description of what this tool does.\"\"\"\n    # your code here\n    return result"}
        onChange={e=>{
          const code=e.target.value;
          setPasteCode(code);
          const nameM=code.match(/def\s+(\w+)\s*\(/);
          const descM=code.match(/"""([\s\S]*?)"""/)||code.match(/'''([\s\S]*?)'''/);
          if(nameM) setPasteToolName(nameM[1]);
          if(descM) setPasteToolDesc(descM[1].trim().split("\n")[0]);
        }}
        style={{...inputS,color:t.warm,lineHeight:1.6,resize:"vertical",tabSize:4,height:"auto",fontFamily:"monospace",marginBottom:8}}/>
      <div style={{display:"flex",gap:8,marginBottom:10}}>
        <div style={{flex:1}}>
          <div style={{fontSize:10,color:t.mut,marginBottom:3}}>Function name</div>
          <input value={pasteToolName} onChange={e=>setPasteToolName(e.target.value)} placeholder="my_tool" style={{...inputS,fontFamily:"monospace"}}/>
        </div>
        <div style={{flex:2}}>
          <div style={{fontSize:10,color:t.mut,marginBottom:3}}>Description (shown to AI)</div>
          <input value={pasteToolDesc} onChange={e=>setPasteToolDesc(e.target.value)} placeholder="What this tool does..." style={inputS}/>
        </div>
      </div>
      <div style={{display:"flex",justifyContent:"flex-end",gap:8}}>
        {pasteCode&&<div style={{fontSize:10,color:t.mut,alignSelf:"center",flex:1}}>
          {pasteCode.split("\n").length} lines · {(pasteCode.length/1024).toFixed(1)} KB
        </div>}
        <button disabled={!pasteCode.trim()||!pasteToolName.trim()} onClick={async()=>{
          const tl={name:pasteToolName.trim(),description:pasteToolDesc.trim(),filename:`${pasteToolName.trim()}.py`,code:pasteCode.trim()};
          try{const r=await fetch(`${API}/api/tools`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(tl)});
            const d=await r.json();setTools(p=>[...p,d]);}
          catch{const id=`t-${Date.now()}`;setTools(p=>[...p,{id,...tl}]);}
          setPasteMode(false);setPasteCode("");setPasteToolName("");setPasteToolDesc("");
        }} style={{...btnS(t.warm),opacity:(!pasteCode.trim()||!pasteToolName.trim())?0.4:1}}>
          ✦ Register Tool
        </button>
      </div>
    </div>}
    <div style={{flex:1,overflowY:"auto",padding:20}}>
      {builtinTools.length>0&&<div style={{marginBottom:16}}>
        <div style={{fontSize:9,fontWeight:700,color:t.mut,letterSpacing:1,textTransform:"uppercase",marginBottom:8}}>Built-in</div>
        {builtinTools.map(bt=>{
          const isExp=expandedBuiltin===bt.id;
          const isCa=bt.id==="codeagent";
          const isDr=bt.id==="deep_research";
          const isQs=bt.id==="quick_search";
          const isCt=bt.id==="conspiracy_research";
          const btCol=isQs?t.f1:isCt?t.err:t.acc;
          const btIcon=isCa?"⚡":isDr?"🔬":isQs?"⚡":isCt?"🕵️":"🔧";
          return <div key={bt.id} style={{...cardS,borderColor:`${btCol}33`,marginBottom:8,cursor:"pointer"}} onClick={()=>setExpandedBuiltin(isExp?null:bt.id)}>
            <div style={{display:"flex",justifyContent:"space-between",alignItems:"center"}}>
              <div style={{display:"flex",alignItems:"center",gap:8}}>
                <div style={{width:32,height:32,borderRadius:8,background:`${btCol}18`,border:`1px solid ${btCol}33`,display:"flex",alignItems:"center",justifyContent:"center",fontSize:16,flexShrink:0}}>{btIcon}</div>
                <div>
                  <div style={{fontSize:13,fontWeight:600,color:btCol}}>{bt.name}</div>
                  <div style={{fontSize:10,color:t.mut}}>{bt.description}</div>
                </div>
              </div>
              <span style={{fontSize:9,color:t.mut,display:"inline-block",transition:"transform .2s",transform:isExp?"rotate(90deg)":"rotate(0deg)"}}>▶</span>
            </div>
            {isExp&&<div style={{marginTop:12,animation:"fadeIn .2s"}}>
              {isCa&&<>
                <div style={{fontSize:10,color:t.dim,marginBottom:6}}>All capabilities — available when CodeAgent is selected:</div>
                <div style={{display:"flex",flexWrap:"wrap",gap:4}}>
                  {["execute_code","run_shell","write_file","read_file","list_files","download_file","download_project","delete_file","research","fetch_url"].map(s=><span key={s} style={{fontSize:10,padding:"3px 8px",borderRadius:8,background:`${t.warm}18`,color:t.warm,border:`1px solid ${t.warm}33`,fontFamily:"monospace"}}>{s}</span>)}
                </div>
              </>}
              {isDr&&<>
                <div style={{fontSize:10,color:t.dim,marginBottom:8}}>Agent-focused research for current docs, unfamiliar APIs, repeated build failures, and implementation blockers:</div>
                <div style={{display:"flex",flexDirection:"column",gap:5}}>
                  <div style={{fontSize:10}}><span style={{color:t.acc,fontFamily:"monospace",fontWeight:600}}>depth</span> <span style={{color:t.mut}}>1–5</span></div>
                  <div style={{paddingLeft:10,display:"flex",flexDirection:"column",gap:2}}>
                    {[["1","Quick check"],["2","Focused coding blocker ← recommended"],["3","Broad unfamiliar tech"],["4","Comprehensive, rarely for agents"],["5","Exhaustive, avoid in fix loops"]].map(([d,desc])=><div key={d} style={{fontSize:9,color:t.mut}}><span style={{color:t.f1,fontFamily:"monospace"}}>{d}</span> — {desc}</div>)}
                  </div>
                  <div style={{fontSize:10,marginTop:2}}><span style={{color:t.acc,fontFamily:"monospace",fontWeight:600}}>mode</span> <span style={{color:t.mut}}>quick · research · compare</span></div>
                  <div style={{fontSize:10}}><span style={{color:t.acc,fontFamily:"monospace",fontWeight:600}}>focus</span> <span style={{color:t.mut}}>version, runtime, failing command, target file</span></div>
                  <div style={{fontSize:10}}><span style={{color:t.acc,fontFamily:"monospace",fontWeight:600}}>topic_b</span> <span style={{color:t.mut}}>second subject for compare mode</span></div>
                </div>
              </>}
              {isCt&&<>
                <div style={{fontSize:10,color:t.dim,marginBottom:6}}>Uncensored investigative research targeting alternative sources, leaked documents, and whistleblower reports:</div>
                <div style={{display:"flex",flexWrap:"wrap",gap:4}}>
                  {["Alternative news","Leaked documents","FOIA releases","Whistleblower reports","Reddit conspiracy","Timeline analysis","Key player mapping","No filtering"].map(s=><span key={s} style={{fontSize:10,padding:"3px 8px",borderRadius:8,background:`${t.err}18`,color:t.err,border:`1px solid ${t.err}33`}}>{s}</span>)}
                </div>
              </>}
              {isQs&&<>
                <div style={{fontSize:10,color:t.dim,marginBottom:6}}>Runs a SearXNG search on each message and shows results as inline cards above chat. Enabled per-conversation via the tool pill in the input bar, or permanently via an Agent.</div>
                <div style={{display:"flex",gap:4,margin:"8px 0 4px",padding:3,border:`1px solid ${t.brd}28`,borderRadius:8,background:`${t.bgDeep}88`,width:"fit-content"}}>
                  {["speed","balanced","quality"].map(m=><button key={m} onClick={e=>{e.stopPropagation();setQuickSearchMode(m);}} style={{fontSize:10,padding:"5px 10px",borderRadius:6,border:"none",background:quickSearchMode===m?`${t.f1}22`:"transparent",color:quickSearchMode===m?t.f1:t.mut,cursor:"pointer",fontFamily:font,fontWeight:quickSearchMode===m?800:600,textTransform:"capitalize"}}>{m}</button>)}
                </div>
                <div style={{display:"flex",flexWrap:"wrap",gap:4,marginTop:4}}>
                  {["YouTube thumbnails","Image previews","Web link cards","Click-to-open"].map(s=><span key={s} style={{fontSize:10,padding:"3px 8px",borderRadius:8,background:`${t.f1}18`,color:t.f1,border:`1px solid ${t.f1}33`}}>{s}</span>)}
                </div>
              </>}
            </div>}
          </div>;
        })}
      </div>}
      <div style={{marginBottom:16}}>
        <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",marginBottom:8}}>
          <div style={{fontSize:9,fontWeight:700,color:t.mut,letterSpacing:1,textTransform:"uppercase"}}>Connectors</div>
          <button onClick={refreshConnectors} style={{...btnS(t.mut),fontSize:10,padding:"4px 8px"}}>Refresh</button>
        </div>
        <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(280px,1fr))",gap:10,marginBottom:10}}>
          <div style={{...cardS,borderColor:`${t.acc}33`}}>
            <div style={{fontSize:12,fontWeight:800,color:t.acc,marginBottom:8}}>MCP Server</div>
            <div style={{display:"grid",gridTemplateColumns:"1fr 110px",gap:8,marginBottom:8}}>
              <input value={mcpDraft.name} onChange={e=>setMcpDraft(p=>({...p,name:e.target.value}))} placeholder="GitHub MCP" style={inputS}/>
              <select value={mcpDraft.transport} onChange={e=>setMcpDraft(p=>({...p,transport:e.target.value}))} style={inputS}>
                <option value="stdio">stdio</option><option value="http">http</option><option value="streamable_http">streamable_http</option><option value="sse">sse</option>
              </select>
            </div>
            <input value={mcpDraft.command} onChange={e=>setMcpDraft(p=>({...p,command:e.target.value}))} placeholder="Command for stdio, e.g. npx" style={{...inputS,marginBottom:8,fontFamily:"monospace"}}/>
            <input value={mcpDraft.url} onChange={e=>setMcpDraft(p=>({...p,url:e.target.value}))} placeholder="HTTP endpoint for Streamable HTTP/SSE" style={{...inputS,marginBottom:8,fontFamily:"monospace"}}/>
            <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8,marginBottom:8}}>
              <div><div style={{fontSize:9,color:t.mut,fontWeight:800,textTransform:"uppercase",letterSpacing:.5,margin:"0 0 4px 2px"}}>Args JSON array</div><textarea value={mcpDraft.args} onChange={e=>setMcpDraft(p=>({...p,args:e.target.value}))} rows={3} placeholder={'["-y","@modelcontextprotocol/server-github"]'} style={{...inputS,fontFamily:"monospace",resize:"vertical"}}/></div>
              <div><div style={{fontSize:9,color:t.mut,fontWeight:800,textTransform:"uppercase",letterSpacing:.5,margin:"0 0 4px 2px"}}>Env placeholders</div><textarea value={mcpDraft.env} onChange={e=>setMcpDraft(p=>({...p,env:e.target.value}))} rows={3} placeholder={'{"GITHUB_TOKEN":"GITHUB_TOKEN"}'} style={{...inputS,fontFamily:"monospace",resize:"vertical"}}/></div>
            </div>
            <div style={{marginBottom:8}}><div style={{fontSize:9,color:t.mut,fontWeight:800,textTransform:"uppercase",letterSpacing:.5,margin:"0 0 4px 2px"}}>HTTP headers</div><textarea value={mcpDraft.headers} onChange={e=>setMcpDraft(p=>({...p,headers:e.target.value}))} rows={2} placeholder={'{"Authorization":"Bearer ${TOKEN}"}'} style={{...inputS,fontFamily:"monospace",resize:"vertical"}}/></div>
            <label style={{display:"flex",alignItems:"center",gap:7,fontSize:10,color:t.mut,marginBottom:8}}><input type="checkbox" checked={mcpDraft.allow_private} onChange={e=>setMcpDraft(p=>({...p,allow_private:e.target.checked}))}/> Allow private/local URL</label>
            <button disabled={connectorBusy==="create:mcp"} onClick={async()=>{
              setConnectorBusy("create:mcp");
              try{
                const payload={name:mcpDraft.name.trim()||"MCP Server",transport:mcpDraft.transport,command:mcpDraft.command.trim(),url:mcpDraft.url.trim(),args:parseConnectorJson(mcpDraft.args,[],"MCP args"),env:parseConnectorJson(mcpDraft.env,{},"MCP env"),headers:parseConnectorJson(mcpDraft.headers,{},"MCP headers"),allow_private:mcpDraft.allow_private};
                const r=await fetch(`${API}/api/mcp-servers`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
                const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
                setMcpDraft({name:"",transport:"stdio",command:"",url:"",args:"[]",env:"{}",headers:"{}",allow_private:false});
                notify({type:"success",text:"MCP server saved"});await refreshConnectors();
              }catch(e){notify({type:"error",text:"MCP save failed",detail:e.message||String(e)});}
              finally{setConnectorBusy("");}
            }} style={{...btnS(t.acc),opacity:connectorBusy==="create:mcp"?0.5:1}}>Add MCP</button>
          </div>
          <div style={{...cardS,borderColor:`${t.f1}33`}}>
            <div style={{fontSize:12,fontWeight:800,color:t.f1,marginBottom:8}}>OpenAPI Connector</div>
            <input value={openapiDraft.name} onChange={e=>setOpenapiDraft(p=>({...p,name:e.target.value}))} placeholder="Service name" style={{...inputS,marginBottom:8}}/>
            <input value={openapiDraft.spec_url} onChange={e=>setOpenapiDraft(p=>({...p,spec_url:e.target.value}))} placeholder="OpenAPI JSON/YAML URL" style={{...inputS,marginBottom:8,fontFamily:"monospace"}}/>
            <input value={openapiDraft.base_url} onChange={e=>setOpenapiDraft(p=>({...p,base_url:e.target.value}))} placeholder="Base URL override (optional)" style={{...inputS,marginBottom:8,fontFamily:"monospace"}}/>
            <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8,marginBottom:8}}>
              <div><div style={{fontSize:9,color:t.mut,fontWeight:800,textTransform:"uppercase",letterSpacing:.5,margin:"0 0 4px 2px"}}>Auth JSON</div><textarea value={openapiDraft.auth} onChange={e=>setOpenapiDraft(p=>({...p,auth:e.target.value}))} rows={4} placeholder={'{"type":"bearer","token_placeholder":"GITHUB_TOKEN"}'} style={{...inputS,fontFamily:"monospace",resize:"vertical"}}/></div>
              <div><div style={{fontSize:9,color:t.mut,fontWeight:800,textTransform:"uppercase",letterSpacing:.5,margin:"0 0 4px 2px"}}>Headers JSON</div><textarea value={openapiDraft.headers} onChange={e=>setOpenapiDraft(p=>({...p,headers:e.target.value}))} rows={4} placeholder={'{"X-Api-Key":"${API_KEY}"}'} style={{...inputS,fontFamily:"monospace",resize:"vertical"}}/></div>
            </div>
            <label style={{display:"flex",alignItems:"center",gap:7,fontSize:10,color:t.mut,marginBottom:8}}><input type="checkbox" checked={openapiDraft.allow_private} onChange={e=>setOpenapiDraft(p=>({...p,allow_private:e.target.checked}))}/> Allow private/local URL</label>
            <button disabled={connectorBusy==="create:openapi"} onClick={async()=>{
              setConnectorBusy("create:openapi");
              try{
                const payload={name:openapiDraft.name.trim()||"OpenAPI Connector",spec_url:openapiDraft.spec_url.trim(),base_url:openapiDraft.base_url.trim(),auth:parseConnectorJson(openapiDraft.auth,{},"OpenAPI auth"),headers:parseConnectorJson(openapiDraft.headers,{},"OpenAPI headers"),allow_private:openapiDraft.allow_private};
                const r=await fetch(`${API}/api/openapi-connectors`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
                const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
                setOpenapiDraft({name:"",spec_url:"",base_url:"",auth:"{}",headers:"{}",allow_private:false});
                notify({type:"success",text:"OpenAPI connector saved"});await refreshConnectors();
              }catch(e){notify({type:"error",text:"OpenAPI save failed",detail:e.message||String(e)});}
              finally{setConnectorBusy("");}
            }} style={{...btnS(t.f1),opacity:connectorBusy==="create:openapi"?0.5:1}}>Add OpenAPI</button>
          </div>
        </div>
        {[...mcpServers.map(x=>({...x,kind:"mcp"})),...openapiConnectors.map(x=>({...x,kind:"openapi"}))].map(c=>{
          const col=c.kind==="mcp"?t.acc:t.f1;const busy=connectorBusy===`${c.kind}:${c.id}`;const basePath=c.kind==="mcp"?"mcp-servers":"openapi-connectors";
          return <div key={`${c.kind}:${c.id}`} style={{...cardS,borderColor:`${col}33`,marginBottom:8}}>
            <div style={{display:"flex",alignItems:"center",gap:8}}>
              <div style={{width:30,height:30,borderRadius:8,background:`${col}18`,border:`1px solid ${col}33`,display:"flex",alignItems:"center",justifyContent:"center",color:col}}>↔</div>
              <div style={{flex:1,minWidth:0}}>
                <div style={{fontSize:12,fontWeight:800,color:col,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{c.name}</div>
                <div style={{fontSize:10,color:t.mut}}>{c.kind.toUpperCase()} · {c.health||"unknown"} · {c.tool_count||0} tools</div>
              </div>
              <button disabled={busy} onClick={()=>discoverConnector(c.kind,c.id)} style={{...btnS(col),fontSize:10,padding:"4px 8px",opacity:busy?0.5:1}}>{busy?"Checking":"Discover"}</button>
              <button onClick={async()=>{await fetch(`${API}/api/${basePath}/${c.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled:!c.enabled})});await refreshConnectors();}} style={{...btnS(c.enabled?t.ok:t.mut),fontSize:10,padding:"4px 8px"}}>{c.enabled?"On":"Off"}</button>
              <button onClick={async()=>{await fetch(`${API}/api/${basePath}/${c.id}`,{method:"DELETE"}).catch(()=>{});await refreshConnectors();}} style={{...btnS(t.err),fontSize:10,padding:"4px 8px"}}><IC.Trash/></button>
            </div>
            {c.last_error&&<div style={{fontSize:10,color:t.err,marginTop:7,whiteSpace:"pre-wrap"}}>{c.last_error}</div>}
          </div>;
        })}
        {connectorTools.length>0&&<div style={{display:"flex",flexWrap:"wrap",gap:4,marginTop:8}}>
          {connectorTools.slice(0,40).map(tl=><span key={tl.id} title={tl.description} style={{fontSize:10,padding:"3px 8px",borderRadius:8,background:`${t.acc}12`,border:`1px solid ${t.acc}28`,color:t.acc}}>{tl.name||tl.display_name}</span>)}
          {connectorTools.length>40&&<span style={{fontSize:10,color:t.mut,padding:"3px 8px"}}>+{connectorTools.length-40} more</span>}
        </div>}
      </div>
      {!tools.length&&!pasteMode&&<div style={{textAlign:"center",padding:40,color:t.mut,fontSize:12}}>
        <div style={{fontSize:28,marginBottom:8}}>⚙️</div>
        <div style={{fontWeight:600,marginBottom:4}}>No custom tools yet</div>
        <div>Paste Python code or upload a .py file to create a tool the AI can call.</div>
      </div>}
      {tools.map(tl=>{
        const snippet=(tl.code||"").split("\n").slice(0,2).join("\n");
        return <div key={tl.id} style={{...cardS,borderColor:editTool===tl.id?`${t.warm}55`:`${t.brd}44`}}>
          <div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",marginBottom:editTool===tl.id?8:0}}>
            <div style={{display:"flex",alignItems:"center",gap:8,flex:1,minWidth:0}}>
              <div style={{width:32,height:32,borderRadius:8,background:`${t.warm}22`,border:`1px solid ${t.warm}44`,display:"flex",alignItems:"center",justifyContent:"center",color:t.warm,flexShrink:0}}><IC.Code/></div>
              <div style={{flex:1,minWidth:0}}>
                <div style={{fontSize:13,fontWeight:600,color:t.text}}>{tl.name}</div>
                {tl.description&&<div style={{fontSize:10,color:t.mut,marginTop:1,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{tl.description}</div>}
                {!editTool!==tl.id&&snippet&&<pre style={{margin:"4px 0 0",fontSize:9,color:`${t.warm}88`,fontFamily:"monospace",overflow:"hidden",whiteSpace:"nowrap",textOverflow:"ellipsis",maxWidth:"100%"}}>{snippet}</pre>}
              </div>
            </div>
            <div style={{display:"flex",gap:4,flexShrink:0,marginLeft:8}}>
              <button onClick={()=>{if(editTool===tl.id){saveTool(tl);}setEditTool(editTool===tl.id?null:tl.id);}} style={btnS(editTool===tl.id?t.ok:t.mut)}>{editTool===tl.id?"Save":"Edit"}</button>
              <button onClick={()=>{fetch(`${API}/api/tools/${tl.id}`,{method:"DELETE"}).catch(()=>{});setTools(p=>p.filter(x=>x.id!==tl.id));}} style={btnS(t.err)}><IC.Trash/></button>
            </div>
          </div>
          {editTool===tl.id&&<div style={{animation:"fadeIn .2s"}}>
            <div style={{display:"flex",gap:8,marginBottom:8}}>
              <input value={tl.name} onChange={e=>setTools(p=>p.map(x=>x.id===tl.id?{...x,name:e.target.value}:x))} placeholder="Name" style={{...inputS,flex:1,fontFamily:"monospace"}}/>
              <input value={tl.description||""} onChange={e=>setTools(p=>p.map(x=>x.id===tl.id?{...x,description:e.target.value}:x))} placeholder="Description (shown to AI)" style={{...inputS,flex:2}}/>
            </div>
            <textarea value={tl.code||""} onChange={e=>setTools(p=>p.map(x=>x.id===tl.id?{...x,code:e.target.value}:x))} rows={14}
              style={{...inputS,color:t.warm,lineHeight:1.6,resize:"vertical",tabSize:4,height:"auto",fontFamily:"monospace"}}/>
          </div>}
        </div>;
      })}
    </div>
  </div>

      :panel==="research"?(()=>{const tmpl=researchTemplates.find(x=>x.id===researchDraft.report_type)||researchTemplates[0]||{id:"analyst",label:"Analyst Report",default_depth:4,sections:[]};const report=activeResearch;const body=cleanResearchMarkdown(researchLiveMarkdown||report?.report_markdown||"").trim();const sources=report?.sources||[];const findings=report?.findings||[];const metrics=report?.metrics||{};const audit=metrics.audit||{};const statusMeta=s=>{const k=String(s||"queued").toLowerCase();const m={complete:[t.ok,"Complete"],running:[t.acc,"Running"],queued:[t.warm,"Queued"],failed:[t.err,"Failed"],cancelled:[t.mut,"Cancelled"]};return m[k]||m.queued;};const tierMeta=tier=>{const k=Number(tier??2);const m={0:["Primary",t.ok],1:["Investigative",t.warm],2:["General",t.mut],3:["Fact-check",t.f1]};return m[k]||m[2];};const fmtAudit=x=>typeof x==="string"?x:`${x?.finding_id?`Finding #${x.finding_id}: `:""}${x?.issue||x?.note||x?.summary||JSON.stringify(x)}`;const eventLabel=(ev,i)=>{const d=ev.data||{};if(ev.type==="research_phase")return `${d.label||d.phase||"Phase"}${d.detail?` - ${d.detail}`:""}`;if(ev.type==="research_source_found")return `Found [S${d.index||d.source_index||"?"}] ${d.title||d.url||"source"}`;if(ev.type==="research_source_read")return `Read [S${d.source_index||"?"}] ${d.title||d.url||"source"}${d.chars?` (${Math.max(1,Math.round(d.chars/1000))}k chars)`:""}`;if(ev.type==="research_finding")return `Extracted Finding #${d.finding_id||i+1}${d.claim?`: ${d.claim}`:""}`;if(ev.type==="research_audit")return `Audit complete${d.coverage_score!==undefined?` - coverage ${d.coverage_score}/100`:""}`;if(ev.type==="research_done")return d.summary?`Report complete - ${d.summary}`:"Report complete";if(ev.type==="research_error")return d.error||d.status?`Stopped - ${d.error||d.status}`:"Research stopped";return d.label||d.status||d.message||d.title||d.phase||ev.type;};const fmtTarget=(v,target)=>target?`${v||0}/${target}`:(v||0);const activeStatus=statusMeta(report?.status);const reportStatus=String(report?.status||"").toLowerCase();const reportLive=["queued","running"].includes(reportStatus);const reportStartedMs=_parseUtcishMs(report?.created_at)||_parseUtcishMs((researchEvents||[]).find(e=>e.type==="research_started")?.ts)||_parseUtcishMs((researchEvents||[]).find(e=>e.type==="research_started")?.timestamp);const elapsedSeconds=reportLive&&reportStartedMs?Math.max(Math.floor((activityNow-reportStartedMs)/1000),Math.round(metrics.elapsed||0)):Math.round(metrics.elapsed||0);const elapsedLabel=elapsedSeconds>0?`${elapsedSeconds}s`:"--";const filteredReports=researchReports.filter(r=>!researchReportFilter||`${r.title||""} ${r.query||""} ${r.summary||""}`.toLowerCase().includes(researchReportFilter.toLowerCase()));const sectionHeadings=[...(report?.outline?.sections||[])].map(s=>s.heading||s).filter(Boolean);const cardS={background:`${t.surface}80`,border:`1px solid ${t.brd}33`,borderRadius:10,padding:14,display:"flex",flexDirection:"column",gap:9};const cardHeadS={fontSize:11,fontWeight:800,color:t.acc,textTransform:"uppercase",letterSpacing:.6};return <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
    <div style={{padding:"14px 20px",borderBottom:`1px solid ${t.brd}28`,display:"flex",justifyContent:"space-between",alignItems:"center",gap:12}}>
      <div style={{display:"flex",alignItems:"center",gap:8,minWidth:0}}><IC.Search/><span style={{fontSize:14,fontWeight:800,letterSpacing:1,textTransform:"uppercase",color:t.acc}}>Deep Research</span>{researchRunning&&<span style={{fontSize:10,padding:"3px 8px",borderRadius:8,background:`${t.acc}14`,border:`1px solid ${t.acc}33`,color:t.acc}}>live</span>}<div style={{display:"flex",gap:3,marginLeft:8,padding:3,border:`1px solid ${t.brd}28`,borderRadius:8,background:`${t.bgDeep}88`}}>{[["new","New"],["reports",researchReports.length?`Reports ${researchReports.length}`:"Reports"]].map(([id,label])=><button key={id} onClick={()=>setResearchView(id)} style={{fontSize:10,padding:"5px 9px",borderRadius:6,border:"none",background:researchView===id?`${t.acc}22`:"transparent",color:researchView===id?t.acc:t.mut,cursor:"pointer",fontFamily:font,fontWeight:researchView===id?800:600}}>{label}</button>)}</div></div>
      {researchView==="reports"&&<div style={{display:"flex",gap:6,alignItems:"center"}}>
        {activeResearchId&&researchRunning&&<button onClick={()=>cancelResearchReport(activeResearchId)} style={btnS(t.err)}><IC.Stop/> Stop</button>}
        {activeResearchId&&<button onClick={()=>rerunResearchReport(activeResearchId)} style={btnS(t.acc)}><IC.Refresh/> Rerun</button>}
        {body&&<button onClick={exportResearchMarkdown} style={btnS(t.warm)}><IC.Download/> Markdown</button>}
        {body&&<button onClick={exportResearchPdf} style={btnS(t.mut)}><IC.Download/> PDF</button>}
        {body&&<button onClick={printResearchReport} style={btnS(t.mut)}><IC.External/> Print</button>}
      </div>}
    </div>
    {researchView==="new"?
    <div style={{flex:1,overflowY:"auto",minHeight:0,padding:"28px 24px"}}>
      <div style={{maxWidth:960,margin:"0 auto",display:"flex",flexDirection:"column",gap:14}}>
        <div style={cardS}>
          <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",gap:8}}>
            <span style={cardHeadS}>New Report</span>
            <span style={{fontSize:10,color:t.mut}}>{tmpl.label}</span>
          </div>
          <textarea value={researchDraft.query} onChange={e=>setResearchDraft(p=>({...p,query:e.target.value}))} placeholder="Research topic or question" rows={4} style={{...inputS,resize:"vertical",fontSize:14,lineHeight:1.45}}/>
          <input value={researchDraft.focus||""} onChange={e=>setResearchDraft(p=>({...p,focus:e.target.value}))} placeholder="Optional focus" style={{...inputS,fontSize:12}}/>
          <div style={{display:"flex",justifyContent:"flex-end"}}>
            <button onClick={startResearchReport} disabled={researchLoading||researchRunning} style={{...btnS(t.acc),justifyContent:"center",minWidth:160,padding:"9px 18px",opacity:(researchLoading||researchRunning)?0.55:1}}><IC.Zap/> Start</button>
          </div>
        </div>
        <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(300px,1fr))",gap:12,alignItems:"start"}}>
          <div style={{display:"flex",flexDirection:"column",gap:12}}>
          <div style={cardS}>
            <span style={cardHeadS}>Report</span>
            <select value={researchDraft.report_type} onChange={e=>{const nt=researchTemplates.find(x=>x.id===e.target.value);setResearchDraft(p=>({...p,report_type:e.target.value,depth:nt?.default_depth||p.depth||3}));}} style={{...inputS,fontSize:12}}>
              {(researchTemplates.length?researchTemplates:[tmpl]).map(rt=>{const ico={analyst:"📊",academic:"🎓",decision:"⚖️",market:"📈",technical:"🛠️",timeline:"🗓️",digest:"🗂️"}[rt.id]||"🔬";return <option key={rt.id} value={rt.id}>{ico} {rt.label}</option>;})}
            </select>
            <select value={researchDraft.depth||3} onChange={e=>setResearchDraft(p=>({...p,depth:parseInt(e.target.value)||3}))} title="Research depth" style={{...inputS,fontSize:11,padding:"8px 7px"}}>
              <option value={1}>⚡ Quick</option>
              <option value={2}>🧭 Overview</option>
              <option value={3}>🔎 Standard</option>
              <option value={4}>📚 Comprehensive</option>
              <option value={5}>🧠 Exhaustive</option>
            </select>
          </div>
          <div style={cardS}>
            <span style={cardHeadS}>Sources & Notes</span>
          <textarea value={researchInputText} onChange={e=>setResearchInputText(e.target.value)} placeholder="Optional pasted notes, constraints, or source excerpts" rows={4} style={{...inputS,resize:"vertical",fontSize:11,lineHeight:1.45}}/>
          <div style={{display:"flex",gap:6,alignItems:"center",flexWrap:"wrap"}}>
            <label style={{...btnS(t.f1),justifyContent:"center",flex:1}}><IC.Upload/> Add Files<input ref={researchFileRef} type="file" multiple accept=".pdf,.txt,.md,.csv,.json,.html,.py,.js,.ts" style={{display:"none"}} onChange={e=>{handleResearchFiles(e.target.files);e.target.value="";}}/></label>
          </div>
          {(researchDraft.inputs||[]).length>0&&<div style={{display:"flex",flexDirection:"column",gap:4}}>
            {(researchDraft.inputs||[]).map((inp,i)=><div key={`${inp.name}-${i}`} style={{display:"flex",alignItems:"center",gap:6,padding:"5px 7px",border:`1px solid ${t.brd}22`,borderRadius:7,background:`${t.surface}55`}}>
              <span style={{fontSize:10,color:t.f1,flex:1,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{inp.name}</span>
              <span style={{fontSize:9,color:t.mut}}>{Math.round((inp.content||"").length/1000)}k</span>
              <button onClick={()=>setResearchDraft(p=>({...p,inputs:(p.inputs||[]).filter((_,ix)=>ix!==i)}))} style={{background:"none",border:"none",color:t.err,cursor:"pointer",display:"flex",padding:2}}><IC.X/></button>
            </div>)}
          </div>}
          {kbs.length>0&&<div style={{display:"flex",flexDirection:"column",gap:4}}>
            <div style={{fontSize:9,fontWeight:800,color:t.mut,textTransform:"uppercase",letterSpacing:.5}}>Knowledge Bases</div>
            <div style={{display:"flex",flexWrap:"wrap",gap:4}}>
              {kbs.slice(0,8).map(kb=>{const on=(researchDraft.kb_ids||[]).includes(kb.id);return <button key={kb.id} onClick={()=>setResearchDraft(p=>({...p,kb_ids:on?(p.kb_ids||[]).filter(x=>x!==kb.id):[...(p.kb_ids||[]),kb.id]}))} style={{fontSize:9,padding:"4px 7px",borderRadius:7,border:`1px solid ${on?t.acc:t.brd}33`,background:on?`${t.acc}18`:`${t.surface}55`,color:on?t.acc:t.mut,cursor:"pointer",fontFamily:font}}>{kb.name}</button>;})}
            </div>
          </div>}
          </div>
          </div>
          <div style={{display:"flex",flexDirection:"column",gap:12}}>
          <div style={cardS}>
            <span style={cardHeadS}>Model</span>
          <ModelPicker value={researchDraft.model||researchSelectableModels[0]||""} onChange={v=>setResearchDraft(p=>({...p,model:v}))} models={researchSelectableModels} modelDetails={modelDetails} t={t} font={font}/>
          <div style={{display:"flex",gap:5,flexWrap:"wrap"}}>
            {localMoeResearchModels.length>0&&<button onClick={()=>applyResearchPreset("balanced_moe")} style={{...btnS(t.acc),fontSize:9}}>Balanced MoE</button>}
            {deepMoeResearchModel&&<button onClick={()=>applyResearchPreset("deep_moe")} style={{...btnS(t.f1),fontSize:9}}>Deep MoE</button>}
            {cloudResearchModels.length>0&&<button onClick={()=>applyResearchPreset("cloud")} style={{...btnS(t.pink),fontSize:9}}>Cloud Research</button>}
            {smallResearchRoleModel&&<button onClick={()=>applyResearchPreset("fast_planning")} style={{...btnS(t.mut),fontSize:9}}>Fast Planning</button>}
          </div>
          <details style={{border:`1px solid ${t.brd}22`,borderRadius:8,padding:"7px 9px",background:`${t.bgDeep}88`}}>
            <summary style={{cursor:"pointer",fontSize:10,fontWeight:800,color:t.mut,textTransform:"uppercase",letterSpacing:.5}}>Role Models</summary>
            <div style={{display:"flex",flexDirection:"column",gap:7,marginTop:8}}>
              <div style={{fontSize:9,color:t.mut,textTransform:"uppercase",letterSpacing:.5}}>Planner</div>
              <ModelPicker value={researchDraft.planner_model||""} onChange={v=>setResearchDraft(p=>({...p,planner_model:v}))} models={["",...researchSelectableModels]} modelDetails={modelDetails} t={t} font={font}/>
              <div style={{fontSize:9,color:t.mut,textTransform:"uppercase",letterSpacing:.5}}>Auditor</div>
              <ModelPicker value={researchDraft.auditor_model||""} onChange={v=>setResearchDraft(p=>({...p,auditor_model:v}))} models={["",...researchSelectableModels]} modelDetails={modelDetails} t={t} font={font}/>
            </div>
          </details>
          </div>
          </div>
          <div style={{display:"flex",flexDirection:"column",gap:12}}>
          <div style={cardS}>
            <label style={{...cardHeadS,color:t.mut,display:"block"}}>Context Window: <span style={{color:t.acc}}>{researchNumCtx.toLocaleString()}</span></label>
            <input type="range" min="8192" max="131072" step="2048" value={researchNumCtx} onChange={e=>setResearchNumCtx(parseInt(e.target.value))} style={{width:"100%",accentColor:t.acc}}/>
            <div style={{fontSize:9,color:t.mut,marginTop:4,lineHeight:1.4}}>
              {researchNumCtx<=16384?<><b style={{color:t.acc}}>Compact (&le;16K)</b> — evidence is clamped hard; fine for depth 1–2 reports.</>
              :researchNumCtx<=65536?<><b style={{color:t.acc}}>Recommended (32–64K)</b> — full evidence budgets through depth 4 fit without truncation.</>
              :<><b style={{color:t.acc}}>Maximum (64–128K)</b> — full depth-5 evidence; uses more VRAM while a report runs.</>}
            </div>
            <div style={{fontSize:9,color:selectedResearchIsCloud?t.pink:(selectedResearchCtx&&researchNumCtx>selectedResearchCtx?t.warm:t.mut),marginTop:5,lineHeight:1.4}}>
              {selectedResearchIsCloud?<>Cloud model selected — local context window only affects Ollama role models.</>
              :selectedResearchCtx&&researchNumCtx>selectedResearchCtx?<><b>Context exceeds selected model ({formatModelCtx(selectedResearchCtx)} ctx).</b> <button onClick={()=>setResearchNumCtx(clampResearchCtx(selectedResearchCtx))} style={{background:"none",border:"none",color:t.acc,cursor:"pointer",fontFamily:font,fontSize:9,padding:0}}>Clamp</button></>
              :selectedResearchIsMoe?<><b style={{color:t.acc}}>MoE expert model</b> — active experts are lighter than dense parameter size, but long research contexts still use KV cache.</>
              :parseInt(researchDraft.depth||3)>=4&&selectedResearchCtx&&selectedResearchCtx<=40960?<><b style={{color:t.warm}}>Depth {researchDraft.depth} on {formatModelCtx(selectedResearchCtx)} ctx</b> — evidence may be clamped; use a larger-context model for exhaustive reports.</>
              :<>Selected model context: {selectedResearchCtx?`${formatModelCtx(selectedResearchCtx)} ctx`:"unknown"}.</>}
            </div>
          </div>
          </div>
        </div>
      </div>
    </div>
    :<div style={{flex:1,display:"grid",gridTemplateColumns:"290px minmax(0,1fr) 320px",overflow:"hidden",minHeight:0}}>
      <div style={{borderRight:`1px solid ${t.brd}24`,overflowY:"auto",padding:14,display:"flex",flexDirection:"column",gap:12}}>
        <div style={{display:"flex",alignItems:"center",gap:6}}>
          <input value={researchReportFilter} onChange={e=>setResearchReportFilter(e.target.value)} placeholder="Filter reports" style={{...inputS,fontSize:11,padding:"7px 9px"}}/>
          <button onClick={()=>refreshResearchReports()} style={btnS(t.mut)}><IC.Refresh/></button>
        </div>
        <div style={{display:"flex",flexDirection:"column",gap:7}}>
          {filteredReports.map(r=>{const sm=statusMeta(r.status);const active=r.id===activeResearchId;return <div key={r.id} onClick={()=>loadResearchReport(r.id)} style={{padding:10,borderRadius:8,border:`1px solid ${active?sm[0]:t.brd}33`,background:active?`${sm[0]}10`:`${t.surface}66`,cursor:"pointer",display:"flex",flexDirection:"column",gap:5}}>
            <div style={{display:"flex",alignItems:"center",gap:6,minWidth:0}}>
              <span style={{width:7,height:7,borderRadius:"50%",background:sm[0],boxShadow:r.status==="running"?`0 0 8px ${sm[0]}99`:"none",flexShrink:0}}/>
              <span style={{fontSize:12,fontWeight:800,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",flex:1}}>{r.title||r.query}</span>
              {workspaces.length>0&&<select value="" title="Add to workspace" onClick={e=>e.stopPropagation()} onChange={e=>{const wsId=e.target.value;if(wsId)addResearchReportToWorkspace(r.id,wsId);}} style={{maxWidth:92,fontSize:9,padding:"3px 5px",borderRadius:6,border:`1px solid ${t.brd}33`,background:t.bgDeep,color:t.mut,fontFamily:font}}><option value="">Workspace</option>{workspaces.map(ws=><option key={ws.id} value={ws.id}>{ws.name}</option>)}</select>}
              <button onClick={e=>{e.stopPropagation();deleteResearchReport(r.id);}} style={{background:"none",border:"none",color:t.err,cursor:"pointer",display:"flex",padding:2,opacity:.75}}><IC.Trash/></button>
            </div>
            <div style={{fontSize:10,color:t.mut,lineHeight:1.35,display:"-webkit-box",WebkitLineClamp:2,WebkitBoxOrient:"vertical",overflow:"hidden"}}>{r.summary||r.query}</div>
            <div style={{display:"flex",gap:5,alignItems:"center",fontSize:9,color:t.dim}}>
              <span>{sm[1]}</span><span>·</span><span>{r.report_type}</span><span>·</span><span>{r.source_count||0} sources</span>
            </div>
          </div>;})}
          {!filteredReports.length&&<div style={{textAlign:"center",padding:18,color:t.mut,fontSize:11,border:`1px dashed ${t.brd}33`,borderRadius:8}}>No research reports yet.</div>}
        </div>
      </div>
      <div style={{overflowY:"auto",padding:"18px 26px",minWidth:0}}>
        {report?<div style={{maxWidth:960,margin:"0 auto"}}>
          <div style={{display:"flex",alignItems:"flex-start",justifyContent:"space-between",gap:14,marginBottom:14,paddingBottom:12,borderBottom:`1px solid ${t.brd}22`}}>
            <div style={{minWidth:0}}>
              <div style={{display:"flex",alignItems:"center",gap:7,marginBottom:5}}>
                <span style={{fontSize:10,fontWeight:900,textTransform:"uppercase",letterSpacing:.7,color:activeStatus[0]}}>{activeStatus[1]}</span>
                <span style={{fontSize:10,color:t.mut}}>{report.report_type}</span>
                {metrics.coverage_score!==undefined&&<span title="Evidence coverage score" style={{fontSize:10,color:t.acc}}>coverage {metrics.coverage_score}/100</span>}
                {activeResearchId&&workspaces.length>0&&<select value="" title="Add report to workspace" onChange={e=>{const wsId=e.target.value;if(wsId)addResearchReportToWorkspace(activeResearchId,wsId);}} style={{fontSize:9,padding:"2px 5px",borderRadius:6,border:`1px solid ${t.brd}33`,background:t.bgDeep,color:t.mut,fontFamily:font}}><option value="">Add to workspace</option>{workspaces.map(ws=><option key={ws.id} value={ws.id}>{ws.name}</option>)}</select>}
              </div>
              <h1 style={{fontSize:24,lineHeight:1.15,margin:"0 0 6px",color:t.text,letterSpacing:0}}>{report.title||report.query}</h1>
              <div style={{fontSize:12,color:t.mut,lineHeight:1.45}}>{report.query}</div>
            </div>
          </div>
          {sectionHeadings.length>0&&<div style={{display:"flex",flexWrap:"wrap",gap:5,marginBottom:14}}>
            {sectionHeadings.slice(0,10).map((h,i)=><span key={i} style={{fontSize:9,padding:"3px 7px",borderRadius:7,background:`${t.acc}10`,border:`1px solid ${t.acc}22`,color:t.acc}}>{h}</span>)}
          </div>}
          {body?<div className="research-report-print" style={{fontSize:fontSize,lineHeight:1.72,color:t.text}}>
            <MDWrap>{md(body)}</MDWrap>
          </div>:<ResearchLiveStatus t={t} font={font} events={researchEvents} metrics={metrics} sources={sources} researchRunning={researchRunning} elapsedLabel={elapsedLabel} eventLabel={eventLabel}/>}
        </div>:<div style={{height:"100%",display:"flex",alignItems:"center",justifyContent:"center",color:t.mut,fontSize:12}}>Select a report or start a new research run.</div>}
      </div>
      <div style={{borderLeft:`1px solid ${t.brd}24`,overflowY:"auto",padding:14,display:"flex",flexDirection:"column",gap:12}}>
        <div style={{display:"grid",gridTemplateColumns:"repeat(2,1fr)",gap:8}}>
          {[["Sources",fmtTarget(sources.length||metrics.source_count||0,metrics.target_sources),t.acc],["Pages",fmtTarget(metrics.pages_read||0,metrics.target_pages),t.f1],["Searches",fmtTarget(metrics.searches||0,metrics.target_queries),t.warm],["Elapsed",elapsedLabel,t.mut]].map(([l,v,c])=><div key={l} style={{padding:10,borderRadius:8,border:`1px solid ${c}2e`,background:`${c}0d`}}>
            <div style={{fontSize:18,fontWeight:900,color:c,lineHeight:1}}>{v}</div><div style={{fontSize:8,color:t.mut,textTransform:"uppercase",letterSpacing:.7,marginTop:5}}>{l}</div>
          </div>)}
        </div>
        {audit&&Object.keys(audit).length>0&&<div style={{border:`1px solid ${t.warm}24`,borderRadius:8,padding:11,background:`${t.warm}08`}}>
          <div style={{fontSize:10,fontWeight:900,color:t.warm,textTransform:"uppercase",letterSpacing:.6,marginBottom:7}}>Audit</div>
          {audit.audit_method==="deterministic_fallback"&&<div style={{fontSize:10,color:t.warm,lineHeight:1.4,marginBottom:8}}>Audit generated from deterministic fallback</div>}
          {["finding_issues","weaknesses","contradictions","missing_evidence","source_quality_notes"].map(k=>(audit[k]||[]).length?<div key={k} style={{marginBottom:8}}>
            <div style={{fontSize:9,color:t.mut,textTransform:"uppercase",letterSpacing:.5,marginBottom:3}}>{k.replaceAll("_"," ")}</div>
            {(audit[k]||[]).slice(0,3).map((x,i)=><div key={i} style={{fontSize:10,color:t.dim,lineHeight:1.4,marginBottom:3}}>• {fmtAudit(x)}</div>)}
          </div>:null)}
        </div>}
        <div>
          <div style={{fontSize:10,fontWeight:900,color:t.acc,textTransform:"uppercase",letterSpacing:.6,marginBottom:7}}>Findings</div>
          <div style={{display:"flex",flexDirection:"column",gap:7}}>
            {findings.slice(0,10).map((f,i)=>{const fid=f.finding_id||i+1;const strength=f.evidence_strength||"";return <div key={i} style={{padding:9,border:`1px solid ${t.brd}24`,borderRadius:8,background:`${t.surface}66`}}>
              <div style={{display:"flex",alignItems:"center",gap:5,marginBottom:5}}>
                <span style={{fontSize:8,fontWeight:900,color:t.acc,textTransform:"uppercase",letterSpacing:.5}}>Finding #{fid}</span>
                {f.confidence&&<span style={{fontSize:8,padding:"1px 5px",borderRadius:5,background:`${t.f1}10`,border:`1px solid ${t.f1}22`,color:t.f1}}>{f.confidence}</span>}
                {strength&&<span style={{fontSize:8,padding:"1px 5px",borderRadius:5,background:`${t.warm}10`,border:`1px solid ${t.warm}22`,color:t.warm}}>{strength}</span>}
              </div>
              <div style={{fontSize:11,fontWeight:800,color:t.text,lineHeight:1.35,marginBottom:5}}>{f.claim||f.title||"Finding"}</div>
              <div style={{fontSize:10,color:t.mut,lineHeight:1.4}}>{f.evidence||f.implication||""}</div>
              {f.caveat&&<div style={{fontSize:9,color:t.warm,lineHeight:1.35,marginTop:5}}>Caveat: {f.caveat}</div>}
              {(f.source_ids||[]).length>0&&<div style={{display:"flex",gap:3,flexWrap:"wrap",marginTop:6}}>{f.source_ids.map(s=><span key={s} style={{fontSize:8,padding:"2px 5px",borderRadius:5,background:`${t.acc}12`,color:t.acc,border:`1px solid ${t.acc}22`}}>{s}</span>)}</div>}
            </div>;})}
            {!findings.length&&<div style={{fontSize:11,color:t.mut}}>Findings appear after extraction.</div>}
          </div>
        </div>
        <div>
          <div style={{fontSize:10,fontWeight:900,color:t.f1,textTransform:"uppercase",letterSpacing:.6,marginBottom:7}}>Sources</div>
          <div style={{display:"flex",flexDirection:"column",gap:6}}>
            {sources.slice(0,35).map(src=>{const tm=tierMeta(src.tier);return <a key={`${src.index}-${src.url||src.title}`} href={src.url||undefined} target={src.url?"_blank":undefined} rel="noreferrer" style={{textDecoration:"none",padding:9,border:`1px solid ${t.brd}22`,borderRadius:8,background:`${t.surface}66`,display:"block"}}>
              <div style={{display:"flex",alignItems:"center",gap:6,marginBottom:4}}>
                <span style={{fontSize:9,fontWeight:900,color:t.f1,flexShrink:0}}>[S{src.index||src.source_index}]</span>
                <span style={{fontSize:10,fontWeight:800,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",flex:1}}>{src.title||src.url||"Uploaded source"}</span>
                <span style={{fontSize:8,padding:"1px 5px",borderRadius:5,background:`${tm[1]}10`,border:`1px solid ${tm[1]}22`,color:tm[1],flexShrink:0}}>{tm[0]}</span>
              </div>
              <div style={{fontSize:9,color:t.mut,lineHeight:1.35,display:"-webkit-box",WebkitLineClamp:2,WebkitBoxOrient:"vertical",overflow:"hidden"}}>{src.snippet||src.url||""}</div>
            </a>;})}
            {!sources.length&&<div style={{fontSize:11,color:t.mut}}>Sources appear during search.</div>}
          </div>
        </div>
        <div>
          <div style={{fontSize:10,fontWeight:900,color:t.mut,textTransform:"uppercase",letterSpacing:.6,marginBottom:7}}>Run Timeline</div>
          <div style={{display:"flex",flexDirection:"column",gap:5}}>
            {(researchEvents||[]).filter(e=>e.type!=="research_token").slice(-40).map((ev,i)=>{const c=ev.type==="research_error"?t.err:ev.type==="research_done"?t.ok:ev.type==="research_source_found"?t.f1:ev.type==="research_audit"?t.warm:t.acc;return <div key={i} style={{display:"flex",gap:7,alignItems:"flex-start",fontSize:10,color:t.dim,lineHeight:1.35}}>
              <span style={{width:6,height:6,borderRadius:"50%",background:c,marginTop:5,flexShrink:0}}/>
              <span>{eventLabel(ev,i)}</span>
            </div>;})}
          </div>
        </div>
      </div>
    </div>}
  </div>;})()

      :panel==="council"?(()=>{const personaProfiles=mcs.filter(isPersonaProfile);const panelS={flex:1,display:"flex",flexDirection:"column",overflow:"hidden"};const headerS={padding:"14px 20px",borderBottom:`1px solid ${t.brd}28`,display:"flex",justifyContent:"space-between",alignItems:"center",gap:12,background:`${t.surface}36`};const sectionS={background:`${t.surface}5C`,border:`1px solid ${t.brd}26`,borderRadius:8,padding:12};const kickerS={fontSize:10,fontWeight:900,color:t.mut,textTransform:"uppercase",letterSpacing:.7};const metaS={fontSize:10,color:t.mut,lineHeight:1.45};const chipS=(c=t.acc)=>({display:"inline-flex",alignItems:"center",gap:4,padding:"2px 7px",borderRadius:6,background:`${c}12`,border:`1px solid ${c}32`,color:c,fontSize:9,fontWeight:800,whiteSpace:"nowrap"});const segmentS=(on,c)=>({padding:"5px 9px",borderRadius:7,border:`1px solid ${on?c:t.brd}38`,background:on?`${c}16`:`${t.surface}55`,color:on?c:t.mut,cursor:"pointer",fontFamily:font,fontSize:10,fontWeight:800});const nativeSelectS={...inputS,fontSize:11,padding:"8px 10px",background:t.bgDeep,color:t.text};const memberProfile=m=>m?.model_config_id?personaProfiles.find(mc=>mc.id===m.model_config_id):null;const memberView=m=>{const mc=memberProfile(m);return{profile:mc,missing:!!m?.model_config_id&&!mc,name:mc?.name||m.persona_name||m.model?.split(":")[0]||"Council member",model:mc?.base_model||m.model||"",prompt:mc?.system_prompt||m.system_prompt||"",avatar:mc?profileAvatar(mc):"",linked:!!mc};};const updateMemberLocal=(cid,mid,patch)=>setCouncils(p=>p.map(c=>c.id===cid?{...c,members:(c.members||[]).map(m=>m.id===mid?{...m,...patch}:m)}:c));const saveMember=async(council,member)=>{const linked=editMemberForm.model_config_id?personaProfiles.find(mc=>mc.id===editMemberForm.model_config_id):null;const up={model:linked?.base_model||editMemberForm.model||member.model||models[0]||"",model_config_id:editMemberForm.model_config_id||"",system_prompt:linked?.system_prompt??editMemberForm.system_prompt??"",persona_name:linked?.name??editMemberForm.persona_name??""};await fetch(`${API}/api/councils/members/${member.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(up)}).catch(()=>{});updateMemberLocal(council.id,member.id,up);setEditMember(null);};const addCustomMember=async(council)=>{if(!models.length)return;const r=await fetch(`${API}/api/councils/${council.id}/members`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model:models[0],model_config_id:"",system_prompt:"",persona_name:""})});const m=await r.json();setCouncils(p=>p.map(c=>c.id===council.id?{...c,members:[...(c.members||[]),m]}:c));setEditMember(m.id);setEditMemberForm({model:m.model||models[0]||"",model_config_id:"",system_prompt:"",persona_name:""});};const addPersonaMember=async(council)=>{const mc=personaProfiles[0];if(!mc)return;const r=await fetch(`${API}/api/councils/${council.id}/members`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model:mc.base_model||models[0]||"",model_config_id:mc.id,system_prompt:mc.system_prompt||"",persona_name:mc.name||""})});const m=await r.json();setCouncils(p=>p.map(c=>c.id===council.id?{...c,members:[...(c.members||[]),m]}:c));setEditMember(m.id);setEditMemberForm({model:mc.base_model||models[0]||"",model_config_id:mc.id,system_prompt:mc.system_prompt||"",persona_name:mc.name||""});};return <div style={panelS}>
    <div style={headerS}>
      <div style={{display:"flex",alignItems:"center",gap:10,minWidth:0}}>
        <span style={{width:30,height:30,borderRadius:8,display:"flex",alignItems:"center",justifyContent:"center",background:`${t.pink}14`,border:`1px solid ${t.pink}32`,color:t.pink}}><IC.Council/></span>
        <div style={{minWidth:0}}>
          <div style={{fontSize:14,fontWeight:900,letterSpacing:.8,textTransform:"uppercase",color:t.text}}>Council of AI</div>
          <div style={{fontSize:10,color:t.mut,marginTop:2}}>Run moderated multi-model discussions with custom or persona-backed members.</div>
        </div>
      </div>
      <button onClick={async()=>{
        const r=await fetch(`${API}/api/councils`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:"New Council",host_model:models[0]||"qwen3.5:27b",host_system_prompt:""})});
        const c=await r.json();setCouncils(p=>[c,...p]);setActiveCouncilId(c.id);
      }} style={{...btnS(t.pink),padding:"8px 12px",fontWeight:900}}><IC.Plus/> New Council</button>
    </div>
    <div style={{flex:1,overflowY:"auto",padding:20}}>
      {councilPresets.length>0&&<div style={{...sectionS,marginBottom:14,padding:12}}>
        <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",gap:10,marginBottom:10}}>
          <div style={kickerS}>Quick Start Presets</div>
          <span style={metaS}>{councilPresets.length} templates</span>
        </div>
        <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fill,minmax(190px,1fr))",gap:8}}>
          {councilPresets.map(p=><button key={p.id} onClick={async()=>{
            try{const r=await fetch(`${API}/api/seed/council-preset/${p.id}`,{method:"POST"});if(!r.ok)throw new Error(`HTTP ${r.status}`);const c=await r.json();setCouncils(prev=>[c,...prev]);setActiveCouncilId(c.id);notify({type:"success",text:"Council preset added",detail:p.name});}catch(e){console.error("Preset error:",e);notify({type:"error",text:"Preset failed",detail:e.message||String(e)});}
          }} style={{cursor:"pointer",padding:"10px 12px",border:`1px solid ${t.brd}34`,borderRadius:8,background:`${t.bgDeep}72`,textAlign:"left",display:"flex",flexDirection:"column",gap:4,fontFamily:font}}>
            <span style={{fontSize:12,fontWeight:900,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{p.name}</span>
            <span style={{fontSize:10,color:t.mut,lineHeight:1.35,display:"-webkit-box",WebkitLineClamp:2,WebkitBoxOrient:"vertical",overflow:"hidden"}}>{p.members.join(" · ")}</span>
          </button>)}
        </div>
      </div>}
      {!councils.length&&!councilPresets.length&&<div style={{...sectionS,textAlign:"center",padding:34,color:t.mut,fontSize:12}}>No councils yet. Create one to debate with multiple AIs.</div>}
      <div style={{display:"flex",flexDirection:"column",gap:12}}>
      {councils.map(council=>{const isActive=activeCouncilId===council.id;const memberCount=(council.members||[]).length;const linkedCount=(council.members||[]).filter(m=>!!memberProfile(m)).length;return <div key={council.id} style={{background:isActive?`${t.pink}0B`:`${t.surface}72`,border:`1px solid ${isActive?t.pink:t.brd}${isActive?"52":"30"}`,borderRadius:8,overflow:"hidden",boxShadow:"none"}}>
          <div style={{display:"grid",gridTemplateColumns:"minmax(0,1fr) auto",gap:12,alignItems:"center",padding:"12px 14px",background:isActive?`${t.pink}08`:`${t.bgDeep}38`,borderBottom:isActive?`1px solid ${t.brd}24`:"none"}}>
            <div style={{display:"flex",alignItems:"center",gap:10,minWidth:0}}>
              <span style={{display:"flex",color:isActive?t.pink:t.mut}}><IC.Council/></span>
              <div style={{minWidth:0,flex:1}}>
                <input value={council.name} onChange={e=>setCouncils(p=>p.map(c=>c.id===council.id?{...c,name:e.target.value}:c))}
                  onBlur={()=>fetch(`${API}/api/councils/${council.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:council.name})}).catch(()=>{})}
                  style={{...inputS,fontSize:14,fontWeight:900,background:"transparent",border:"none",padding:0,color:t.text}}/>
                <div style={{display:"flex",gap:6,flexWrap:"wrap",marginTop:5}}>
                  <span style={chipS(t.acc)}>{memberCount} member{memberCount===1?"":"s"}</span>
                  {linkedCount>0&&<span style={chipS(t.pink)}>{linkedCount} persona-linked</span>}
                  <span style={chipS(t.warm)}>{(council.debate_rounds||0)===0?"single round":`${council.debate_rounds} rebuttal`}</span>
                </div>
              </div>
            </div>
            <div style={{display:"flex",gap:5,alignItems:"center",justifyContent:"flex-end"}}>
              <button onClick={()=>setActiveCouncilId(isActive?null:council.id)} style={btnS(isActive?t.pink:t.mut)}>{isActive?<><IC.ChevDown/> Collapse</>:<><IC.ChevDown/> Expand</>}</button>
              <button onClick={async()=>{await fetch(`${API}/api/councils/${council.id}`,{method:"DELETE"}).catch(()=>{});setCouncils(p=>p.filter(c=>c.id!==council.id));if(activeCouncilId===council.id)setActiveCouncilId(null);}} style={btnS(t.err)}><IC.Trash/></button>
            </div>
          </div>
          {isActive&&<div style={{padding:14,display:"flex",flexDirection:"column",gap:12}}>
            <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(280px,1fr))",gap:12,alignItems:"stretch"}}>
              <div style={sectionS}>
                <div style={{display:"flex",alignItems:"center",gap:6,marginBottom:9,color:t.warm}}><IC.Crown/><span style={kickerS}>Host / Moderator</span></div>
                <ModelPicker value={council.host_model||models[0]||""} onChange={v=>{setCouncils(p=>p.map(c=>c.id===council.id?{...c,host_model:v}:c));fetch(`${API}/api/councils/${council.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({host_model:v})}).catch(()=>{});}} models={models} modelDetails={modelDetails} t={t} font={font} style={{marginBottom:8}}/>
                <textarea value={council.host_system_prompt||""} onChange={e=>{const v=e.target.value;setCouncils(p=>p.map(c=>c.id===council.id?{...c,host_system_prompt:v}:c));}}
                  onBlur={()=>fetch(`${API}/api/councils/${council.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({host_system_prompt:council.host_system_prompt||""})}).catch(()=>{})}
                  placeholder="Moderator system prompt (optional)" rows={4}
                  style={{...inputS,resize:"vertical",fontSize:11,color:t.dim,lineHeight:1.45,minHeight:88}}/>
              </div>
              <div style={{...sectionS,display:"flex",flexDirection:"column",justifyContent:"center",gap:10}}>
                <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",gap:10}}>
                  <div>
                    <div style={kickerS}>Debate Rounds</div>
                    <div style={metaS}>{(council.debate_rounds||0)===0?"Members answer once, then the host synthesizes.":(council.debate_rounds||0)===1?"One rebuttal pass before synthesis.":`${council.debate_rounds} rebuttal passes before synthesis.`}</div>
                  </div>
                  <div style={{fontSize:26,fontWeight:900,color:t.pink,lineHeight:1,minWidth:36,textAlign:"right"}}>{council.debate_rounds||0}</div>
                </div>
                <input type="range" min={0} max={5} value={council.debate_rounds||0}
                  onChange={e=>{const v=parseInt(e.target.value);setCouncils(p=>p.map(c=>c.id===council.id?{...c,debate_rounds:v}:c));}}
                  onMouseUp={e=>{const v=parseInt(e.target.value);fetch(`${API}/api/councils/${council.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({debate_rounds:v})}).catch(()=>{});}}
                  onTouchEnd={()=>{const v=council.debate_rounds||0;fetch(`${API}/api/councils/${council.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({debate_rounds:v})}).catch(()=>{});}}
                  style={{width:"100%",accentColor:t.pink}}/>
              </div>
            </div>
            <div style={sectionS}>
              <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",gap:10,marginBottom:10}}>
                <div>
                  <div style={kickerS}>Council Members</div>
                  <div style={metaS}>Use custom prompts or link members to Personas. Linked members use the latest Persona profile when the council runs.</div>
                </div>
                <div style={{display:"flex",gap:6,flexWrap:"wrap",justifyContent:"flex-end"}}>
                  <button onClick={()=>addCustomMember(council)} disabled={!models.length} style={{...btnS(t.acc),opacity:models.length?1:.45}}><IC.Plus/> Custom</button>
                  <button onClick={()=>addPersonaMember(council)} disabled={!personaProfiles.length} title={personaProfiles.length?"Add a Persona member":"Create a Persona first"} style={{...btnS(t.pink),opacity:personaProfiles.length?1:.45}}><IC.User/> Persona</button>
                </div>
              </div>
              <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fill,minmax(250px,1fr))",gap:9}}>
                {(council.members||[]).map(member=>{const isEditing=editMember===member.id;const pts=member.points||0;const ptsColor=pts>0?t.ok:pts<0?t.err:t.mut;const view=memberView(member);const selectedProfile=editMemberForm.model_config_id?personaProfiles.find(mc=>mc.id===editMemberForm.model_config_id):null;return <div key={member.id} style={{background:`${t.bgDeep}78`,border:`1px solid ${view.linked?t.pink:t.brd}${view.linked?"40":"30"}`,borderRadius:8,padding:12,display:"flex",flexDirection:"column",gap:8,minHeight:isEditing?260:148}}>
                  <div style={{display:"flex",alignItems:"flex-start",gap:9,minWidth:0}}>
                    <div style={{width:34,height:34,borderRadius:8,background:`${view.linked?t.pink:t.acc}12`,border:`1px solid ${view.linked?t.pink:t.acc}35`,color:view.linked?t.pink:t.acc,display:"flex",alignItems:"center",justifyContent:"center",overflow:"hidden",flexShrink:0}}>
                      {view.avatar?<img src={avatarSrc(view.avatar)} alt="" style={{width:"100%",height:"100%",objectFit:"cover"}}/>:view.linked?<IC.User/>:<IC.Bot/>}
                    </div>
                    <div style={{minWidth:0,flex:1}}>
                      <div style={{display:"flex",alignItems:"center",gap:6,minWidth:0}}>
                        <span style={{fontSize:12,fontWeight:900,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{view.name}</span>
                        {view.linked&&<span style={chipS(t.pink)}>Persona</span>}
                        {view.missing&&<span style={chipS(t.err)}>Missing</span>}
                      </div>
                      <div style={{fontSize:10,color:view.missing?t.err:t.mut,marginTop:3,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{view.missing?"Persona profile not found":view.model||"No model selected"}</div>
                    </div>
                    <span style={{...chipS(ptsColor),background:pts>0?`${t.ok}12`:pts<0?`${t.err}12`:`${t.mut}0D`}}>{pts>0?"+":""}{pts} pts</span>
                  </div>
                  {isEditing?<>
                    <div style={{display:"flex",gap:6}}>
                      <button onClick={()=>{const v=memberView(member);setEditMemberForm({model:v.model||models[0]||"",model_config_id:"",system_prompt:v.prompt||"",persona_name:v.name||""});}} style={segmentS(!editMemberForm.model_config_id,t.acc)}><IC.Bot/> Custom</button>
                      <button onClick={()=>{const mc=selectedProfile||memberProfile(member)||personaProfiles[0];if(mc)setEditMemberForm({model:mc.base_model||models[0]||"",model_config_id:mc.id,system_prompt:mc.system_prompt||"",persona_name:mc.name||""});}} disabled={!personaProfiles.length} style={{...segmentS(!!editMemberForm.model_config_id,t.pink),opacity:personaProfiles.length?1:.45}}><IC.User/> Persona</button>
                    </div>
                    {editMemberForm.model_config_id?<>
                      <select value={editMemberForm.model_config_id} onChange={e=>{const mc=personaProfiles.find(x=>x.id===e.target.value);setEditMemberForm({model:mc?.base_model||models[0]||"",model_config_id:mc?.id||"",system_prompt:mc?.system_prompt||"",persona_name:mc?.name||""});}} style={nativeSelectS}>
                        {personaProfiles.map(mc=><option key={mc.id} value={mc.id}>{mc.name} · {mc.base_model||"no model"}</option>)}
                      </select>
                      <div style={{padding:9,borderRadius:8,border:`1px solid ${t.pink}24`,background:`${t.pink}08`,display:"flex",flexDirection:"column",gap:4}}>
                        <div style={{fontSize:11,fontWeight:900,color:t.text}}>{selectedProfile?.name||"Persona"}</div>
                        <div style={metaS}>Linked to Persona. This member will use the Persona's latest model and prompt each run.</div>
                        <div style={{display:"flex",gap:5,flexWrap:"wrap"}}>
                          <button onClick={()=>{if(selectedProfile){setPanel("personas");setProfileTab("personas");setEditMc(selectedProfile.id);}}} style={btnS(t.pink)}><IC.Pencil/> Edit Persona</button>
                          <button onClick={()=>{const mc=selectedProfile||memberProfile(member);setEditMemberForm({model:mc?.base_model||member.model||models[0]||"",model_config_id:"",system_prompt:mc?.system_prompt||member.system_prompt||"",persona_name:mc?.name||member.persona_name||""});}} style={btnS(t.mut)}><IC.X/> Detach</button>
                        </div>
                      </div>
                    </>:<>
                      <ModelPicker value={editMemberForm.model||member.model||models[0]||""} onChange={v=>setEditMemberForm(p=>({...p,model:v}))} models={models} modelDetails={modelDetails} t={t} font={font}/>
                      <input value={editMemberForm.persona_name||""} onChange={e=>setEditMemberForm(p=>({...p,persona_name:e.target.value}))} placeholder="Display name" style={{...inputS,fontSize:11}}/>
                      <textarea value={editMemberForm.system_prompt||""} onChange={e=>setEditMemberForm(p=>({...p,system_prompt:e.target.value}))} placeholder="Member system prompt" rows={5} style={{...inputS,resize:"vertical",fontSize:11,lineHeight:1.45,minHeight:104}}/>
                    </>}
                    <div style={{display:"flex",gap:6,marginTop:"auto"}}>
                      <button onClick={()=>saveMember(council,member)} style={{...btnS(t.ok),flex:1,justifyContent:"center",fontWeight:900}}>Save</button>
                      <button onClick={()=>setEditMember(null)} style={btnS(t.mut)}>Cancel</button>
                    </div>
                  </>:<>
                    <div style={{fontSize:10,color:t.dim,lineHeight:1.45,display:"-webkit-box",WebkitLineClamp:3,WebkitBoxOrient:"vertical",overflow:"hidden",minHeight:42}}>{view.prompt||"No member prompt set."}</div>
                    <div style={{display:"flex",gap:5,marginTop:"auto"}}>
                      <button onClick={()=>{const v=memberView(member);setEditMember(member.id);setEditMemberForm({model:v.model||member.model||models[0]||"",model_config_id:member.model_config_id||"",system_prompt:v.prompt||"",persona_name:v.name||""});}} style={{...btnS(t.acc),flex:1,justifyContent:"center",fontSize:10}}><IC.Pencil/> Edit</button>
                      <button onClick={async()=>{await fetch(`${API}/api/councils/members/${member.id}`,{method:"DELETE"}).catch(()=>{});setCouncils(p=>p.map(c=>c.id===council.id?{...c,members:c.members.filter(m=>m.id!==member.id)}:c));}} style={btnS(t.err)}><IC.Trash/></button>
                    </div>
                  </>}
                </div>;})}
                {!(council.members||[]).length&&<div style={{gridColumn:"1/-1",border:`1px dashed ${t.brd}45`,borderRadius:8,padding:"24px 12px",textAlign:"center",color:t.mut,fontSize:11}}>Add a custom member or link a Persona to start.</div>}
              </div>
            </div>
            <div style={{display:"grid",gridTemplateColumns:"minmax(0,1fr) minmax(0,1fr)",gap:8}}>
              <button onClick={async()=>{if(councilReport?.council_id===council.id){setCouncilReport(null);return;}setCouncilReportLoading(true);try{const r=await fetch(`${API}/api/councils/${council.id}/analyze`);if(!r.ok)throw new Error(`HTTP ${r.status}`);const d=await r.json();setCouncilReport(d);}catch(e){console.error(e);notify({type:"error",text:"Council analysis failed",detail:e.message||String(e)});}setCouncilReportLoading(false);}} style={{...btnS(t.acc),justifyContent:"center",padding:"10px 14px",fontSize:12,fontWeight:900}}>
                <IC.BarChart/> {councilReportLoading?"Analyzing...":councilReport?.council_id===council.id?"Hide Report":"Analyze Performance"}
              </button>
              <button onClick={()=>startCouncilChat(council.id)} disabled={!memberCount} style={{...btnS(t.pink),justifyContent:"center",padding:"10px 14px",fontSize:12,fontWeight:900,opacity:memberCount?1:.4}}>
                <IC.Council/> Start Council Chat
              </button>
            </div>
            {councilReport?.council_id===council.id&&<div style={{...sectionS,animation:"fadeIn .3s"}}>
              <div style={{display:"flex",alignItems:"center",gap:6,marginBottom:12,color:t.acc}}><IC.BarChart/><span style={kickerS}>Performance Report</span></div>
              <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(160px,1fr))",gap:8,marginBottom:12}}>
                <div style={{padding:12,borderRadius:8,border:`1px solid ${t.brd}24`,background:`${t.bgDeep}66`}}><div style={{fontSize:24,fontWeight:900,color:t.pink,lineHeight:1}}>{councilReport.total_debates}</div><div style={metaS}>Debates</div></div>
                <div style={{padding:12,borderRadius:8,border:`1px solid ${t.brd}24`,background:`${t.bgDeep}66`}}><div style={{fontSize:24,fontWeight:900,color:t.acc,lineHeight:1}}>{councilReport.total_conversations}</div><div style={metaS}>Sessions</div></div>
                {(councilReport.recommendations||[]).length>0&&<div style={{padding:12,borderRadius:8,border:`1px solid ${t.warm}24`,background:`${t.warm}08`,gridColumn:"span 2"}}>
                  <div style={{...kickerS,color:t.warm,marginBottom:6}}>Recommendations</div>
                  {councilReport.recommendations.map((r,ri)=><div key={ri} style={{fontSize:10,color:t.dim,lineHeight:1.45,marginBottom:3}}>{r}</div>)}
                </div>}
              </div>
              <div style={{display:"flex",flexDirection:"column",gap:7}}>
                {(councilReport.members||[]).map((m,i)=>{const barWidth=councilReport.total_debates>0?Math.max(m.votes_received/councilReport.total_debates*100,4):4;return <div key={m.id} style={{background:`${t.bgDeep}66`,border:`1px solid ${t.brd}28`,borderRadius:8,padding:"9px 10px"}}>
                  <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:6}}>
                    <span style={chipS(i===0?t.ok:i===1?t.acc:t.mut)}>#{i+1}</span>
                    <span style={{fontSize:12,fontWeight:900,color:t.text,flex:1,minWidth:0,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{m.persona_name}</span>
                    <span style={{fontSize:10,color:t.mut,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",maxWidth:260}}>{m.model}</span>
                  </div>
                  <div style={{background:`${t.brd}22`,borderRadius:6,height:14,overflow:"hidden",position:"relative",marginBottom:6}}>
                    <div style={{background:i===0?t.ok:i===1?t.acc:t.mut,height:"100%",width:`${barWidth}%`,borderRadius:6,transition:"width .5s",minWidth:20}}/>
                    <span style={{position:"absolute",left:6,top:0,fontSize:9,fontWeight:900,color:"#fff",lineHeight:"14px"}}>{m.win_rate}% win rate</span>
                  </div>
                  <div style={{display:"flex",gap:8,flexWrap:"wrap",fontSize:10,color:t.dim}}>
                    <span>{m.votes_received} votes</span><span>{m.responses} responses</span><span>{m.avg_response_length} avg chars</span><span style={{color:m.points>0?t.ok:m.points<0?t.err:t.mut}}>{m.points>0?"+":""}{m.points} pts</span>
                  </div>
                  {Object.keys(m.vote_sources||{}).length>0&&<div style={{marginTop:6,display:"flex",gap:4,flexWrap:"wrap"}}>{Object.entries(m.vote_sources).map(([name,count],vi)=><span key={vi} style={chipS(t.pink)}>{name} ({count})</span>)}</div>}
                </div>;})}
              </div>
            </div>}
          </div>}
        </div>;})}
      </div>
    </div>
  </div>;})()
      :panel==="personas"?(()=>{const profiles=mcs.map(mc=>({...mc,parameters:normalizeProfileParams(mc)}));const agentProfiles=profiles.filter(isAgentProfile);const personaProfiles=profiles.filter(isPersonaProfile);const activeProfiles=profileTab==="personas"?personaProfiles:agentProfiles;const activeColor=profileTab==="personas"?t.pink:t.acc;const activeLabel=profileTab==="personas"?"Personas":"Agents";const activeEmpty=profileTab==="personas"?"Create a persona for roleplay, character chat, writing voice, or companion-style conversations.":"Create an agent for coding, research, automation, or specialized work.";const setName=(mc,value)=>updateProfileLocal(mc.id,x=>{const next={...x,name:value};if(getProfileType(x)==="persona"){const params=normalizeProfileParams(next);next.system_prompt=buildPersonaPrompt({name:value,...params.persona});}return next;});const setNumberParam=(id,key,value)=>setProfileParam(id,key,value===""?null:(key==="num_ctx"?parseInt(value)||null:parseFloat(value)));const modelWarning=(mc)=>{const missing=mc.base_model&&models.length&&!models.includes(mc.base_model);if(!mc.base_model||missing)return <div style={{display:"inline-flex",alignItems:"center",gap:5,marginTop:5,padding:"3px 7px",borderRadius:7,background:`${t.warm}12`,border:`1px solid ${t.warm}35`,color:t.warm,fontSize:9,fontWeight:700}}>{!mc.base_model?"No base model selected":`Missing model: ${mc.base_model}`}</div>;return null;};const avatarBox=(mc,isE,color,kind)=>{const avatar=profileAvatar(mc);return <label style={{cursor:isE?"pointer":"default",position:"relative",flexShrink:0}}>
        <div style={{width:44,height:44,borderRadius:kind==="persona"?12:9,background:`${color}18`,border:`1px solid ${color}40`,display:"flex",alignItems:"center",justifyContent:"center",color,overflow:"hidden",fontSize:20}}>
          {avatar?<img src={avatarSrc(avatar)} style={{width:"100%",height:"100%",objectFit:"cover"}} alt=""/>:kind==="persona"?<IC.User/>:<IC.Bot/>}
        </div>
        {isE&&<div style={{position:"absolute",bottom:-2,right:-2,background:color,borderRadius:"50%",width:16,height:16,display:"flex",alignItems:"center",justifyContent:"center",color:"#fff"}}><IC.Image/></div>}
        {isE&&<input type="file" accept="image/*" style={{display:"none"}} onChange={async e=>{
          const f=e.target.files[0];if(!f)return;
          const fd=new FormData();fd.append("file",f);
          try{const r=await fetch(`${API}/api/model-configs/${mc.id}/avatar`,{method:"POST",body:fd});const d=await r.json();const parameters={...normalizeProfileParams(mc),avatar:d.avatar_url};setMcs(p=>p.map(x=>x.id===mc.id?{...x,parameters}:x));}
          catch{const url=URL.createObjectURL(f);const parameters={...normalizeProfileParams(mc),avatar:url};setMcs(p=>p.map(x=>x.id===mc.id?{...x,parameters}:x));}
        }}/>}
      </label>;};const createAgent=async()=>{const mc={name:"New Agent",base_model:models[0]||"",system_prompt:"You are a focused assistant for specialized work. Use your available tools when they help complete the task.",tool_ids:[],kb_ids:[],parameters:{profile_type:"agent",description:"Custom agent for coding, research, automation, or specialized work."}};try{const r=await fetch(`${API}/api/model-configs`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(mc)});const d=await r.json();const norm={...d,parameters:normalizeProfileParams(d)};setMcs(p=>[norm,...p]);setProfileTab("agents");setEditMc(norm.id);}catch{const id=`mc-${Date.now()}`;setMcs(p=>[{id,...mc},...p]);setProfileTab("agents");setEditMc(id);}};const createPersona=async()=>{const persona={description:"",personality:"",appearance:"",scenario:"",first_message:"",example_dialogue:"",lore:"",tags:[],rating:"PG-13",thinking_mode:"auto",advanced_prompt:""};const mc={name:"New Persona",base_model:models[0]||"",system_prompt:buildPersonaPrompt({name:"New Persona",...persona}),tool_ids:[],kb_ids:[],parameters:{profile_type:"persona",persona,description:""}};try{const r=await fetch(`${API}/api/model-configs`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(mc)});const d=await r.json();const norm={...d,parameters:normalizeProfileParams(d)};setMcs(p=>[norm,...p]);setProfileTab("personas");setEditMc(norm.id);}catch{const id=`mc-${Date.now()}`;setMcs(p=>[{id,...mc},...p]);setProfileTab("personas");setEditMc(id);}};const deleteProfile=(mc)=>{fetch(`${API}/api/model-configs/${mc.id}`,{method:"DELETE"}).catch(()=>{});setMcs(p=>p.filter(x=>x.id!==mc.id));if(editMc===mc.id)setEditMc(null);};const renderAgent=(mc)=>{const isE=editMc===mc.id;const params=normalizeProfileParams(mc);const tools=mc.tool_ids||[];const kbIds=mc.kb_ids||[];const hasCoding=tools.includes("codeagent")||isCoderPersonaName(mc.name);const hasResearch=tools.some(x=>x.includes("research"));return <div key={mc.id} style={{...cardS,borderColor:isE?`${t.acc}55`:`${t.brd}44`}}>
        <div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",gap:12,marginBottom:10}}>
          <div style={{display:"flex",alignItems:"flex-start",gap:10,minWidth:0,flex:1}}>
            {avatarBox(mc,isE,t.acc,"agent")}
            <div style={{minWidth:0,flex:1}}>
              {isE?<input value={mc.name} onChange={e=>setName(mc,e.target.value)} style={{...inputS,fontSize:14,fontWeight:700,background:"transparent",border:"none",padding:0,color:t.text,width:"min(360px,100%)"}}/>:<div style={{fontSize:14,fontWeight:700,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{mc.name}</div>}
              <div style={{fontSize:10,color:t.mut,marginTop:2}}>Base: {mc.base_model||"none"}</div>
              {modelWarning(mc)}
              {!isE&&<div style={{fontSize:11,color:t.dim,marginTop:6,lineHeight:1.45,maxWidth:760}}>{profileDescription(mc)}</div>}
            </div>
          </div>
          <div style={{display:"flex",gap:4,flexWrap:"wrap",justifyContent:"flex-end"}}>
            <button onClick={()=>applyProfile(mc)} style={btnS(t.ok)}>Use Agent</button>
            <button onClick={()=>{if(isE)saveProfile(mc);setEditMc(isE?null:mc.id);}} style={btnS(t.mut)}>{isE?"Done":"Edit"}</button>
            <button onClick={()=>deleteProfile(mc)} style={btnS(t.err)}><IC.Trash/></button>
          </div>
        </div>
        {isE?<div style={{animation:"fadeIn .2s"}}>
          <div style={{marginBottom:12}}>
            <label style={labelS}>Base Model</label>
            <ModelPicker value={mc.base_model} onChange={v=>updateProfileLocal(mc.id,x=>({...x,base_model:v}))} models={models} modelDetails={modelDetails} t={t} font={font}/>
          </div>
          <div style={{marginBottom:12}}>
            <label style={labelS}>Agent Description</label>
            <textarea value={params.description||""} onChange={e=>setProfileParam(mc.id,"description",e.target.value)} rows={2} placeholder={profileDescription({...mc,parameters:{...params,description:""}})} style={{...inputS,resize:"vertical",lineHeight:1.5,minHeight:58}}/>
          </div>
          <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(130px,1fr))",gap:8,marginBottom:12}}>
            <div><label style={labelS}>Temperature</label><input type="number" min="0" max="2" step="0.05" value={params.temperature??""} onChange={e=>setNumberParam(mc.id,"temperature",e.target.value)} placeholder="default" style={inputS}/></div>
            <div><label style={labelS}>Top P</label><input type="number" min="0" max="1" step="0.05" value={params.top_p??""} onChange={e=>setNumberParam(mc.id,"top_p",e.target.value)} placeholder="default" style={inputS}/></div>
            <div><label style={labelS}>Context</label><input type="number" min="0" step="1024" value={params.num_ctx??""} onChange={e=>setNumberParam(mc.id,"num_ctx",e.target.value)} placeholder="default" style={inputS}/></div>
          </div>
          <div style={{marginBottom:12}}>
            <label style={labelS}>System Prompt</label>
            <textarea value={mc.system_prompt||""} onChange={e=>updateProfileLocal(mc.id,x=>({...x,system_prompt:e.target.value}))} rows={12} style={{...inputS,resize:"vertical",lineHeight:1.5,minHeight:190}}/>
          </div>
          <div style={{marginBottom:12}}>
            <label style={labelS}><span style={{display:"flex",alignItems:"center",gap:4}}><IC.Tool/> Tools</span></label>
            <div style={{display:"flex",flexDirection:"column",gap:4}}>
              {allTools.map(tl=>{const on=tools.includes(tl.id);const tlCol=tl.id==="quick_search"?t.f1:t.warm;return <div key={tl.id} onClick={()=>updateProfileLocal(mc.id,x=>({...x,tool_ids:on?(x.tool_ids||[]).filter(y=>y!==tl.id):[...(x.tool_ids||[]),tl.id]}))}
                style={{display:"flex",alignItems:"center",gap:8,padding:"8px 10px",background:on?`${tlCol}12`:`${t.surface}44`,border:`1px solid ${on?tlCol:t.brd}33`,borderRadius:8,cursor:"pointer",transition:"all .15s"}}>
                <span style={{color:on?tlCol:t.mut}}>{on?<IC.ToggleOn/>:<IC.ToggleOff/>}</span>
                <span style={{fontSize:12,fontWeight:on?600:400,color:on?t.text:t.dim}}>{tl.name}</span>
                <span style={{fontSize:10,color:t.mut,marginLeft:"auto"}}>{tl.description}</span>
              </div>;})}
              {!allTools.length&&<span style={{fontSize:10,color:t.mut,fontStyle:"italic"}}>Loading tools...</span>}
            </div>
          </div>
          <div>
            <label style={labelS}><span style={{display:"flex",alignItems:"center",gap:4}}><IC.Database/> Knowledge Bases</span></label>
            <div style={{display:"flex",flexDirection:"column",gap:4}}>
              {kbs.map(kb=>{const on=kbIds.includes(kb.id);return <div key={kb.id} onClick={()=>updateProfileLocal(mc.id,x=>({...x,kb_ids:on?(x.kb_ids||[]).filter(y=>y!==kb.id):[...(x.kb_ids||[]),kb.id]}))}
                style={{display:"flex",alignItems:"center",gap:8,padding:"8px 10px",background:on?`${t.f1}12`:`${t.surface}44`,border:`1px solid ${on?t.f1:t.brd}33`,borderRadius:8,cursor:"pointer",transition:"all .15s"}}>
                <span style={{color:on?t.f1:t.mut}}>{on?<IC.ToggleOn/>:<IC.ToggleOff/>}</span>
                <span style={{fontSize:12,fontWeight:on?600:400,color:on?t.text:t.dim}}>{kb.name}</span>
                <span style={{fontSize:10,color:t.mut,marginLeft:"auto"}}>{(kb.files||[]).length} files</span>
              </div>;})}
              {!kbs.length&&<span style={{fontSize:10,color:t.mut,fontStyle:"italic"}}>No KBs — create some in Knowledge Bases</span>}
            </div>
          </div>
        </div>:<div style={{display:"flex",flexWrap:"wrap",gap:5,marginTop:4}}>
          {hasCoding&&<Chip l="Coding" c={t.acc} bg={`${t.acc}15`}/>}
          {hasResearch&&<Chip l="Research" c={t.warm} bg={`${t.warm}15`}/>}
          {isCoderPersonaName(mc.name)&&<Chip l="Workflow" c={t.ok} bg={`${t.ok}15`}/>}
          {tools.map(tid=>{const tl=allTools.find(x=>x.id===tid);if(!tl)return null;const c=tl.id==="quick_search"?t.f1:t.warm;return <Chip key={tid} l={tl.name} c={c} bg={`${c}15`}/>;})}
          {kbIds.length>0&&<Chip l={`${kbIds.length} KB${kbIds.length===1?"":"s"}`} c={t.f1} bg={`${t.f1}15`}/>}
        </div>}
      </div>;};const renderPersona=(mc)=>{const isE=editMc===mc.id;const params=normalizeProfileParams(mc);const p=params.persona||{};const tags=parseTags(p.tags);const kbIds=mc.kb_ids||[];return <div key={mc.id} style={{...cardS,borderColor:isE?`${t.pink}55`:`${t.brd}44`,padding:16}}>
        <div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",gap:12,marginBottom:10}}>
          <div style={{display:"flex",alignItems:"flex-start",gap:11,minWidth:0,flex:1}}>
            {avatarBox(mc,isE,t.pink,"persona")}
            <div style={{minWidth:0,flex:1}}>
              {isE?<input value={mc.name} onChange={e=>setName(mc,e.target.value)} style={{...inputS,fontSize:14,fontWeight:700,background:"transparent",border:"none",padding:0,color:t.text,width:"min(360px,100%)"}}/>:<div style={{fontSize:15,fontWeight:800,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{mc.name}</div>}
              {!isE&&<div style={{fontSize:11,color:t.dim,marginTop:4,lineHeight:1.45,maxWidth:680}}>{profileDescription(mc)}</div>}
              <div style={{fontSize:10,color:t.mut,marginTop:isE?2:6}}>Base: {mc.base_model||"none"}</div>
              {modelWarning(mc)}
              {!isE&&(tags.length>0||p.rating||kbIds.length>0||normalizePersonaThinkingMode(p.thinking_mode)!=="auto")&&<div style={{display:"flex",flexWrap:"wrap",gap:5,marginTop:7}}>
                {tags.map(tag=><Chip key={tag} l={tag} c={t.pink} bg={`${t.pink}12`}/>)}
                {p.rating&&<Chip l={personaRatingLabel(p.rating)} c={t.warm} bg={`${t.warm}12`}/>}
                {normalizePersonaThinkingMode(p.thinking_mode)!=="auto"&&<Chip l={`Thinking ${personaThinkingLabel(p.thinking_mode)}`} c={t.f4} bg={`${t.f4}12`}/>}
                {kbIds.length>0&&<Chip l={`${kbIds.length} KB${kbIds.length===1?"":"s"}`} c={t.f1} bg={`${t.f1}15`}/>}
              </div>}
            </div>
          </div>
          <div style={{display:"flex",gap:4,flexWrap:"wrap",justifyContent:"flex-end"}}>
            <button onClick={()=>applyProfile(mc)} style={btnS(t.ok)}>Use Persona</button>
            <button onClick={()=>{if(isE)saveProfile(mc);setEditMc(isE?null:mc.id);}} style={btnS(t.mut)}>{isE?"Done":"Edit"}</button>
            <button onClick={()=>deleteProfile(mc)} style={btnS(t.err)}><IC.Trash/></button>
          </div>
        </div>
        {isE&&<div style={{animation:"fadeIn .2s"}}>
          <div style={{marginBottom:12}}>
            <label style={labelS}>Base Model</label>
            <ModelPicker value={mc.base_model} onChange={v=>updateProfileLocal(mc.id,x=>({...x,base_model:v}))} models={models} modelDetails={modelDetails} t={t} font={font}/>
          </div>
          <div style={{marginBottom:12}}>
            <label style={labelS}><span style={{display:"flex",alignItems:"center",gap:4}}><IC.Database/> Knowledge Bases</span></label>
            <div style={{display:"flex",flexDirection:"column",gap:4}}>
              {kbs.map(kb=>{const on=kbIds.includes(kb.id);return <div key={kb.id} onClick={()=>updateProfileLocal(mc.id,x=>({...x,kb_ids:on?(x.kb_ids||[]).filter(y=>y!==kb.id):[...(x.kb_ids||[]),kb.id]}))}
                style={{display:"flex",alignItems:"center",gap:8,padding:"8px 10px",background:on?`${t.f1}12`:`${t.surface}44`,border:`1px solid ${on?t.f1:t.brd}33`,borderRadius:8,cursor:"pointer",transition:"all .15s"}}>
                <span style={{color:on?t.f1:t.mut}}>{on?<IC.ToggleOn/>:<IC.ToggleOff/>}</span>
                <span style={{fontSize:12,fontWeight:on?600:400,color:on?t.text:t.dim}}>{kb.name}</span>
                <span style={{fontSize:10,color:t.mut,marginLeft:"auto"}}>{(kb.files||[]).length} files</span>
              </div>;})}
              {!kbs.length&&<span style={{fontSize:10,color:t.mut,fontStyle:"italic"}}>No KBs — create some in Knowledge Bases</span>}
            </div>
          </div>
          <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(220px,1fr))",gap:10,marginBottom:12}}>
            <div><label style={labelS}>Short Description</label><textarea value={p.description||""} onChange={e=>setPersonaField(mc.id,"description",e.target.value)} rows={2} style={{...inputS,resize:"vertical",lineHeight:1.5,minHeight:58}}/></div>
            <div><label style={labelS}>Personality</label><textarea value={p.personality||""} onChange={e=>setPersonaField(mc.id,"personality",e.target.value)} rows={2} style={{...inputS,resize:"vertical",lineHeight:1.5,minHeight:58}}/></div>
            <div><label style={labelS}>Appearance <span style={{color:t.mut,fontWeight:400}}>(used for selfies / photos of this persona)</span></label><textarea value={p.appearance||""} onChange={e=>setPersonaField(mc.id,"appearance",e.target.value)} rows={2} placeholder="Physical appearance: hair, eyes, build, style, typical outfit… The persona uses this to generate photos of itself when asked." style={{...inputS,resize:"vertical",lineHeight:1.5,minHeight:58}}/></div>
          </div>
          <div style={{marginBottom:12}}>
            <label style={labelS}>Scenario</label>
            <textarea value={p.scenario||""} onChange={e=>setPersonaField(mc.id,"scenario",e.target.value)} rows={3} style={{...inputS,resize:"vertical",lineHeight:1.5,minHeight:80}}/>
          </div>
          <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(220px,1fr))",gap:10,marginBottom:12}}>
            <div><label style={labelS}>First Message</label><textarea value={p.first_message||""} onChange={e=>setPersonaField(mc.id,"first_message",e.target.value)} rows={3} style={{...inputS,resize:"vertical",lineHeight:1.5,minHeight:80}}/></div>
            <div><label style={labelS}>Example Dialogue</label><textarea value={p.example_dialogue||""} onChange={e=>setPersonaField(mc.id,"example_dialogue",e.target.value)} rows={3} style={{...inputS,resize:"vertical",lineHeight:1.5,minHeight:80}}/></div>
          </div>
          <div style={{marginBottom:12}}>
            <label style={labelS}>Lore / World Notes</label>
            <textarea value={p.lore||""} onChange={e=>setPersonaField(mc.id,"lore",e.target.value)} rows={3} style={{...inputS,resize:"vertical",lineHeight:1.5,minHeight:80}}/>
          </div>
          <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(150px,1fr))",gap:8,marginBottom:12}}>
            <div><label style={labelS}>Tags</label><input value={tagsToText(p.tags)} onChange={e=>setPersonaField(mc.id,"tags",e.target.value)} placeholder="warm, noir, mentor" style={inputS}/></div>
            <div>
              <label style={labelS}>Rating</label>
              <select value={normalizePersonaRating(p.rating)} onChange={e=>setPersonaField(mc.id,"rating",e.target.value)} style={inputS}>
                {PERSONA_RATINGS.map(([key,label])=><option key={key} value={key}>{label}</option>)}
              </select>
              <div style={{fontSize:9,color:t.mut,lineHeight:1.35,marginTop:4}}>{personaRatingGuidance(p.rating)}</div>
            </div>
            <div>
              <label style={labelS}>Thinking</label>
              <select value={normalizePersonaThinkingMode(p.thinking_mode)} onChange={e=>setPersonaField(mc.id,"thinking_mode",e.target.value)} style={inputS}>
                <option value="auto">🧠 Auto</option>
                <option value="on">💭 On</option>
                <option value="off">⚡ Off</option>
              </select>
              <div style={{fontSize:9,color:t.mut,lineHeight:1.35,marginTop:4}}>{personaThinkingGuidance(p.thinking_mode)}</div>
            </div>
            <div><label style={labelS}>Temperature</label><input type="number" min="0" max="2" step="0.05" value={params.temperature??""} onChange={e=>setNumberParam(mc.id,"temperature",e.target.value)} placeholder="default" style={inputS}/></div>
            <div><label style={labelS}>Top P</label><input type="number" min="0" max="1" step="0.05" value={params.top_p??""} onChange={e=>setNumberParam(mc.id,"top_p",e.target.value)} placeholder="default" style={inputS}/></div>
          </div>
          <div>
            <label style={labelS}>Advanced System Prompt</label>
            <textarea value={p.advanced_prompt||""} onChange={e=>setPersonaField(mc.id,"advanced_prompt",e.target.value)} rows={9} placeholder="Extra instructions appended after the persona fields." style={{...inputS,resize:"vertical",lineHeight:1.5,minHeight:150}}/>
          </div>
        </div>}
      </div>;};return <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
    <div style={{padding:"14px 20px 0",borderBottom:`1px solid ${t.brd}28`,display:"flex",alignItems:"flex-end",justifyContent:"space-between",gap:10,flexWrap:"wrap"}}>
      <div style={{display:"flex",alignItems:"flex-end",gap:4}}>
        <span style={{fontSize:14,fontWeight:800,letterSpacing:1,textTransform:"uppercase",color:t.acc,marginRight:12,paddingBottom:10}}>Agents</span>
        {[["agents",<IC.Cube/>,"Agents",agentProfiles.length],["personas",<IC.User/>,"Personas",personaProfiles.length]].map(([key,ico,label,count])=><button key={key} onClick={()=>{setProfileTab(key);setEditMc(null);}} style={{padding:"8px 16px",borderRadius:"8px 8px 0 0",border:`1px solid ${profileTab===key?(key==="personas"?t.pink:t.acc):t.brd}44`,borderBottom:`2px solid ${profileTab===key?(key==="personas"?t.pink:t.acc):"transparent"}`,background:profileTab===key?`${key==="personas"?t.pink:t.acc}12`:"transparent",color:profileTab===key?(key==="personas"?t.pink:t.acc):t.mut,fontFamily:font,fontSize:12,cursor:"pointer",fontWeight:profileTab===key?800:500,marginBottom:-1,display:"flex",alignItems:"center",gap:6}}>
          {ico}{label}<span style={{fontSize:9,color:t.mut}}>{count}</span>
        </button>)}
      </div>
      <div style={{display:"flex",gap:5,flexWrap:"wrap",paddingBottom:9}}>
        <button onClick={async()=>{
          const ok=await confirmAction({title:"Restore default agents",body:"Restore the built-in agents and persona templates? Existing custom profiles are left alone unless the backend explicitly updates a matching default.",confirmLabel:"Restore Defaults",tone:"warning"});
          if(!ok)return;
          try{
            const r=await fetch(`${API}/api/seed/all-defaults`,{method:"POST"});
            if(!r.ok)throw new Error(`HTTP ${r.status}`);
            const d=await r.json();
            const mc2=await fetch(`${API}/api/model-configs`).then(r=>r.json());setMcs((mc2||[]).map(mc=>({...mc,parameters:normalizeProfileParams(mc)})));
            const names=(d.restored||[]).filter(x=>x.status==="ok").map(x=>x.name).join(", ");
            notify({type:"success",text:"Defaults restored",detail:names||"No defaults changed."});
          }catch(e){notify({type:"error",text:"Restore failed",detail:e.message});}
        }} style={{...btnS(t.acc),fontSize:10}}>Restore Defaults</button>
        <button onClick={createAgent} style={btnS(t.acc)}><IC.Plus/> New Agent</button>
        <button onClick={createPersona} style={btnS(t.pink)}><IC.Plus/> New Persona</button>
        {profileTab==="personas"&&<><input ref={charCardImportRef} type="file" accept=".png,.json,application/json,image/png" style={{display:"none"}} onChange={e=>{if(e.target.files?.[0])importCharacterCard(e.target.files[0]);e.target.value="";}}/>
          <button onClick={()=>charCardImportRef.current?.click()} style={btnS(t.pink)}><IC.Download/> Import Card</button></>}
      </div>
    </div>
    <div style={{padding:"10px 20px",borderBottom:`1px solid ${t.brd}18`,display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(260px,1fr))",gap:8,background:`${t.surface}22`}}>
      <div style={{display:"flex",alignItems:"flex-start",gap:8,padding:"8px 10px",borderRadius:8,border:`1px solid ${t.acc}24`,background:`${t.acc}08`,minWidth:0}}>
        <span style={{display:"flex",color:t.acc,marginTop:1,flexShrink:0}}><IC.Cube/></span>
        <div style={{fontSize:11,lineHeight:1.45,color:t.dim,minWidth:0}}><span style={{fontWeight:800,color:t.acc,textTransform:"uppercase",letterSpacing:.6,fontSize:9}}>Agents</span> are task-driven assistants for coding, research, automation, tools, knowledge bases, and specialized work.</div>
      </div>
      <div style={{display:"flex",alignItems:"flex-start",gap:8,padding:"8px 10px",borderRadius:8,border:`1px solid ${t.pink}24`,background:`${t.pink}08`,minWidth:0}}>
        <span style={{display:"flex",color:t.pink,marginTop:1,flexShrink:0}}><IC.User/></span>
        <div style={{fontSize:11,lineHeight:1.45,color:t.dim,minWidth:0}}><span style={{fontWeight:800,color:t.pink,textTransform:"uppercase",letterSpacing:.6,fontSize:9}}>Personas</span> are voice and roleplay profiles focused on identity, tone, scenario, backstory, and conversational style.</div>
      </div>
    </div>
    <div style={{flex:1,overflowY:"auto",padding:20}}>
      <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:12}}>
        <span style={{display:"flex",color:activeColor}}>{profileTab==="personas"?<IC.User/>:<IC.Cube/>}</span>
        <span style={{fontSize:12,fontWeight:800,textTransform:"uppercase",letterSpacing:.8,color:activeColor}}>{activeLabel}</span>
      </div>
      {!activeProfiles.length&&<div style={{textAlign:"center",padding:42,color:t.mut,fontSize:12,border:`1px dashed ${t.brd}44`,borderRadius:8,background:`${t.surface}33`}}>{activeEmpty}</div>}
      {activeProfiles.map(mc=>profileTab==="personas"?renderPersona(mc):renderAgent(mc))}
    </div>
  </div>;})()

      :panel==="prompts"?<PromptLibraryPanel t={t} btnS={btnS} cardS={cardS} inputS={inputS} prompts={prompts} setPrompts={setPrompts} editPrompt={editPrompt} setEditPrompt={setEditPrompt} setInp={setInp} setPanel={setPanel}/>

      :panel==="models"?<div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
    {/* Toolbar */}
    <div style={{padding:"14px 20px",flexShrink:0,display:"flex",alignItems:"center",gap:14,borderBottom:`1px solid ${t.brd}24`,background:`${t.bgDeep}E8`,minHeight:62,boxSizing:"border-box",flexWrap:"wrap"}}>
      <div style={{display:"flex",alignItems:"center",gap:10,minWidth:180}}>
        <span style={{display:"flex",color:t.acc}}><IC.Database/></span>
        <div>
          <div style={{fontSize:15,fontWeight:900,color:t.acc,letterSpacing:1.2,textTransform:"uppercase",lineHeight:1}}>Models</div>
          <div style={{fontSize:10,color:t.mut,marginTop:3}}>{installedModelCount} installed · {hfModelCount} from Hugging Face</div>
        </div>
      </div>
      <div style={{display:"flex",alignItems:"center",gap:4,padding:3,borderRadius:8,border:`1px solid ${t.brd}28`,background:`${t.surface}44`}}>
        {[["ollama","Ollama","🦙",installedModelCount],["hf","Hugging Face","🤗",hfModelCount],["hyprfit","HyprFit","⚡",null]].map(([key,label,icon,count])=>{
          const active=modelsTab===key;
          return <button key={key} onClick={()=>{setModelsTab(key);setModelParamsOpen(null);setShowModelfile(false);if(key!=="hf")setHfSelected(null);}} style={{padding:"7px 13px",borderRadius:7,border:"none",background:active?`${t.acc}18`:"transparent",color:active?t.acc:t.mut,fontFamily:font,fontSize:12,cursor:"pointer",fontWeight:active?900:700,display:"flex",alignItems:"center",gap:7,transition:"all .15s"}}>
            <span>{icon}</span><span>{label}</span>{count!=null&&<span style={{...mmChipS(active?t.acc:t.mut,active?`${t.acc}12`:`${t.surface}66`),fontSize:8,padding:"1px 5px"}}>{count}</span>}
          </button>;
        })}
      </div>
      <div style={{flex:1}}/>
      <button onClick={modelsTab==="hyprfit"?()=>loadHyprfit(true):refreshModels} title={modelsTab==="hyprfit"?"Refresh HyprFit":"Refresh models"} style={mmIconBtnS(t.acc)}><IC.Refresh/></button>
    </div>

    {/* ── OLLAMA TAB ── */}
    {modelsTab==="ollama"&&<div style={{flex:1,display:"flex",overflow:"hidden",background:`${t.bg}22`}}>

      {/* Left: model list */}
      <div style={{width:"clamp(360px,30vw,460px)",minWidth:340,borderRight:`1px solid ${t.brd}20`,display:"flex",flexDirection:"column",overflow:"hidden",background:`${t.bgDeep}66`}}>
        {/* Search bar */}
        <div style={{padding:"12px 12px 10px",flexShrink:0,borderBottom:`1px solid ${t.brd}16`}}>
          <div style={{display:"flex",gap:8,alignItems:"center",marginBottom:8}}>
            <div style={{position:"relative",flex:1}}>
              <span style={{position:"absolute",left:10,top:"50%",transform:"translateY(-50%)",color:t.mut,display:"flex",opacity:.75}}><IC.Search/></span>
              <input value={modelSearch} onChange={e=>setModelSearch(e.target.value)} placeholder="Search installed models..." style={{...inputS,width:"100%",fontSize:13,padding:"9px 13px 9px 34px",background:t.bgDeep}}/>
            </div>
          </div>
          <div style={{display:"flex",gap:6,alignItems:"center",justifyContent:"space-between"}}>
            <span style={mmKickerS}>Inventory</span>
            <span style={mmMetaS}>{localModelCount} local · {hfModelCount} HF</span>
          </div>
        </div>
        <div style={{flex:1,overflowY:"auto",padding:"10px 12px 14px"}}>
          {(()=>{
            const searchLower=modelSearch.toLowerCase();
            const families={};
            const visibleModels=models.filter(m=>!isCloudModelName(m));
            visibleModels.filter(m=>!searchLower||m.toLowerCase().includes(searchLower)).forEach(m=>{
              const [name,tag="latest"]=m.split(":");
              const base=name.toLowerCase();
              let fam="Other";
              if(base.startsWith("hf.co/"))fam="HuggingFace";
              else if(base.includes("qwen"))fam="Qwen";
              else if(base.includes("llama"))fam="Llama";
              else if(base.includes("mistral")||base.includes("mixtral"))fam="Mistral";
              else if(base.includes("gemma"))fam="Gemma";
              else if(base.includes("phi"))fam="Phi";
              else if(base.includes("deepseek"))fam="DeepSeek";
              else if(base.includes("command"))fam="Command";
              else if(base.includes("wizard")||base.includes("vicuna"))fam="WizardLM";
              else if(base.includes("hermes"))fam="Hermes";
              if(!families[fam])families[fam]=[];
              families[fam].push({m,name,tag});
            });
            const szCol=(tag)=>{const l=(tag||"").toLowerCase();if(l.match(/70b|65b|72b/))return t.err;if(l.match(/30b|32b|34b/))return t.warm;if(l.match(/13b|14b|27b/))return t.acc;if(l.match(/7b|8b/))return t.ok;if(l.match(/1b|3b|4b/))return t.f1;return t.mut;};
            const icon=(n)=>{const b=n.toLowerCase();if(b.startsWith("hf.co/"))return"🤗";if(b.includes("qwen"))return"🌸";if(b.includes("llama"))return"🦙";if(b.includes("mistral")||b.includes("mixtral"))return"💨";if(b.includes("gemma"))return"💎";if(b.includes("phi"))return"φ";if(b.includes("deepseek"))return"🔍";if(b.includes("coder")||b.includes("code"))return"💻";if(b.includes("wizard"))return"🧙";return"🤖";};
            const famEmoji={HuggingFace:"🤗",Qwen:"🌸",Llama:"🦙",Mistral:"💨",Gemma:"💎",Phi:"φ",DeepSeek:"🔍",Command:"⚡",WizardLM:"🧙",Hermes:"🏛",Other:"🤖"};
            const allItems=Object.values(families).flat();
            if(!visibleModels.length)return <div style={{...mmPanelS,padding:"28px 14px",color:t.mut,fontSize:12,textAlign:"center",lineHeight:1.6}}>No installed models yet.<br/>Pull one from Ollama to start.</div>;
            if(allItems.length===0&&searchLower)return <div style={{...mmPanelS,padding:"28px 14px",color:t.mut,fontSize:12,textAlign:"center"}}>No models matching "{modelSearch}"</div>;
            return Object.entries(families).map(([fam,items])=><div key={fam} style={{marginBottom:16}}>
              <div style={{fontSize:11,color:t.mut,textTransform:"uppercase",letterSpacing:.7,margin:"0 2px 7px",display:"flex",alignItems:"center",gap:7}}>
                <span style={{opacity:.82}}>{famEmoji[fam]||"🤖"}</span><span style={{fontWeight:900}}>{fam}</span><span style={{opacity:.6}}>{items.length}</span>
              </div>
              {items.map(({m,name,tag})=>{
                const sc=szCol(tag);const sel=modelParamsOpen===m;
                const hasCustom=Object.values(modelParams[m]||{}).some(v=>v&&v!=="default");
                const md=modelDetails[m]||{};
                const quant=(md.details?.quantization_level||"").toUpperCase();
                const paramSz=md.details?.parameter_size||"";
                const diskSz=md.size?fmtSize(md.size):"";
                const caps=capTags(m);
                return <div key={m} onClick={()=>{const newSel=sel?null:m;setModelParamsOpen(newSel);setShowModelfile(false);setFixTemplateMsg(null);setFixTemplateFamily("");if(newSel&&!modelInfoCache[newSel]){setModelInfoLoading(true);fetch(`${API}/api/models/${encodeURIComponent(newSel)}/info`).then(r=>r.json()).then(d=>setModelInfoCache(p=>({...p,[newSel]:d}))).catch(()=>{}).finally(()=>setModelInfoLoading(false));}}}
                  style={{display:"flex",alignItems:"center",gap:12,padding:"13px 14px",borderRadius:8,marginBottom:7,cursor:"pointer",background:sel?`${t.acc}14`:`${t.surface}40`,border:`1px solid ${sel?t.acc:t.brd}${sel?"55":"18"}`,transition:"all .15s",boxShadow:sel?`inset 3px 0 0 ${t.acc}`:"none"}}>
                  <span style={{fontSize:18,flexShrink:0,width:24,textAlign:"center"}}>{icon(name)}</span>
                  <div style={{flex:1,minWidth:0}}>
                    <div style={{display:"flex",alignItems:"center",gap:6}}>
                      <span style={{fontSize:14,fontWeight:800,color:sel?t.acc:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",lineHeight:1.25}}>{name}</span>
                      {m.startsWith("hf.co/")&&<span style={mmChipS(t.warm)}>HF</span>}
                      {hasCustom&&<div style={{width:6,height:6,borderRadius:"50%",background:t.acc,flexShrink:0}} title="Custom parameters"/>}
                    </div>
                    <div style={{display:"flex",alignItems:"center",gap:6,marginTop:5,flexWrap:"wrap"}}>
                      {paramSz&&<span style={{fontSize:11,color:sc,fontWeight:800}}>{paramSz}</span>}
                      {!paramSz&&<span style={{fontSize:11,color:sc,fontWeight:800}}>{tag}</span>}
                      {quant&&(()=>{const qc=quantColor(quant);return <span style={{...mmChipS(qc),fontSize:10,padding:"2px 7px"}}>{quant}</span>;})()}
                      {diskSz&&<span style={{fontSize:10,color:t.mut,opacity:.85}}>{diskSz}</span>}
                    </div>
                    {caps.length>0&&<div style={{display:"flex",gap:3,marginTop:4,flexWrap:"wrap"}}>
                      {caps.slice(0,3).map(c=><span key={c.label} style={{...mmChipS(c.color),fontSize:10,padding:"2px 7px",fontWeight:700}}>{c.emoji} {c.label}</span>)}
                    </div>}
                  </div>
                  <button onClick={e=>{e.stopPropagation();deleteModel(m);}} title="Delete model" style={{...mmIconBtnS(t.err),opacity:.35,background:"transparent",borderColor:"transparent"}} onMouseEnter={e=>e.currentTarget.style.opacity=1} onMouseLeave={e=>e.currentTarget.style.opacity=.35}><IC.Trash/></button>
                  <span style={{fontSize:13,color:sel?t.acc:t.mut,flexShrink:0,transition:"transform .2s",transform:sel?"rotate(90deg)":"none"}}>›</span>
                </div>;
              })}
            </div>);
          })()}
        </div>
      </div>

      {/* Right: parameter detail */}
      <div style={{flex:1,overflowY:"auto",padding:"24px 28px 40px"}}>
        {!modelParamsOpen?
          /* Global defaults shown when nothing is selected */
          <div style={{maxWidth:860}}>
            <div style={{...mmPanelStrongS,padding:18,marginBottom:16}}>
              <div style={{display:"flex",alignItems:"flex-start",justifyContent:"space-between",gap:16,flexWrap:"wrap"}}>
                <div style={{minWidth:220,flex:"1 1 260px"}}>
                  <div style={mmKickerS}>Ollama pull</div>
                  <div style={{fontSize:17,fontWeight:900,color:t.text,marginTop:4}}>Pull Ollama model</div>
                  <div style={{fontSize:12,color:t.mut,marginTop:5,lineHeight:1.45}}>Install a model by name, then tune its overrides from the inventory list.</div>
                </div>
                <div style={{display:"flex",gap:8,alignItems:"center",flex:"1 1 340px",minWidth:280}}>
                  <input ref={pullInputRef} value={pullName} onChange={e=>setPullName(e.target.value)} placeholder="llama3.1:8b" onKeyDown={e=>e.key==="Enter"&&pullModel()} style={{...inputS,flex:1,minWidth:180,fontSize:13,padding:"9px 11px",background:t.bgDeep}}/>
                  <button onClick={pullModel} disabled={!pullName.trim()} style={{...btnS(t.ok),padding:"9px 14px",fontSize:12,flexShrink:0,opacity:pullName.trim()?1:.55}}><IC.Download/> Pull</button>
                </div>
              </div>
            </div>
            <div style={{...mmPanelStrongS,padding:20,marginBottom:16}}>
              <div style={{display:"flex",alignItems:"flex-start",justifyContent:"space-between",gap:14,marginBottom:18,flexWrap:"wrap"}}>
                <div>
                  <div style={mmKickerS}>Defaults</div>
                  <div style={{fontSize:20,fontWeight:900,color:t.text,marginTop:4}}>Global model behavior</div>
                  <div style={{fontSize:12,color:t.mut,marginTop:5,maxWidth:520,lineHeight:1.5}}>Applied to every model unless that model has its own override. Select a model from the inventory to tune context and sampling individually.</div>
                </div>
                <div style={{display:"flex",gap:8,flexWrap:"wrap",justifyContent:"flex-end"}}>
                  <span style={mmChipS(t.acc)}>{models.filter(m=>!isCloudModelName(m)).length} installed</span>
                  <span style={mmChipS(t.warm)}>{Object.keys(modelParams).filter(k=>Object.values(modelParams[k]||{}).some(v=>v&&v!=="default")).length} overrides</span>
                </div>
              </div>
            {[
              ["Default Temperature","range","0","2","0.05",defaultTemp,v=>setDefaultTemp(parseFloat(v)),v=>v.toFixed(2),"Precise","Balanced","Creative","temperature"],
              ["Default Top-P","range","0.1","1","0.05",defaultTopP,v=>setDefaultTopP(parseFloat(v)),v=>v.toFixed(2),"Focused","Balanced","Diverse","top_p"],
            ].map(([lbl,type,min,max,step,val,setter,fmt,l1,l2,l3,tipKey])=>
              <div key={lbl} style={{...mmPanelS,padding:"14px 16px",marginBottom:12}}>
                <div style={{display:"flex",justifyContent:"space-between",alignItems:"baseline",marginBottom:8}}>
                  <span style={{fontSize:12,color:t.dim,textTransform:"uppercase",letterSpacing:.6,fontWeight:900}}>{lbl} {tipKey&&<Tip k={tipKey} t={t}/>}</span>
                  <span style={{fontSize:16,fontWeight:700,color:t.acc,fontVariantNumeric:"tabular-nums"}}>{fmt(val)}</span>
                </div>
                <input type="range" min={min} max={max} step={step} value={val} onChange={e=>setter(e.target.value)} style={{width:"100%",accentColor:t.acc,cursor:"pointer",height:6}}/>
                <div style={{display:"flex",justifyContent:"space-between",fontSize:10,color:t.mut,marginTop:4}}><span>{l1}</span><span>{l2}</span><span>{l3}</span></div>
              </div>
            )}
            </div>
            <div style={{...mmPanelS,padding:"14px 16px",display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(180px,1fr))",gap:12}}>
              <div>
                <div style={mmKickerS}>Next step</div>
                <div style={{fontSize:12,color:t.dim,marginTop:5,lineHeight:1.5}}>Select an installed model to set its context window, tool template, and per-model sampling overrides.</div>
              </div>
              <div>
                <div style={mmKickerS}>Global context</div>
                <div style={{fontSize:18,fontWeight:900,color:t.acc,marginTop:5}}>{numCtx>=1024?`${(numCtx/1024).toFixed(0)}K`:numCtx}</div>
                <div style={mmMetaS}>Configured from Settings</div>
              </div>
            </div>
          </div>
        :
          /* Per-model parameters */
          (()=>{
            const m=modelParamsOpen;const [name,tag="latest"]=m.split(":");const mp=modelParams[m]||{};
            const hasCustom=Object.values(mp).some(v=>v&&v!=="default");
            const icon=(n)=>{const b=n.toLowerCase();if(b.includes("qwen"))return"🌸";if(b.includes("llama"))return"🦙";if(b.includes("mistral"))return"💨";if(b.includes("gemma"))return"💎";if(b.includes("phi"))return"φ";if(b.includes("deepseek"))return"🔍";if(b.includes("coder")||b.includes("code"))return"💻";return"🤖";};
            const szCol=(tag)=>{const l=(tag||"").toLowerCase();if(l.match(/70b|65b|72b/))return t.err;if(l.match(/30b|32b|34b/))return t.warm;if(l.match(/13b|14b|27b/))return t.acc;if(l.match(/7b|8b/))return t.ok;if(l.match(/1b|3b|4b/))return t.f1;return t.mut;};
            const sc=szCol(tag);
            const md=modelDetails[m]||{};const det=md.details||{};
            const diskSz=md.size?fmtSize(md.size):"";
            const quant=(det.quantization_level||"").toUpperCase();
            const paramSz=det.parameter_size||"";
            const family=det.family||"";
            const format=(det.format||"").toUpperCase();
            const modifiedAt=md.modified_at?(()=>{try{return new Date(md.modified_at).toLocaleDateString();}catch{return md.modified_at;}})():"";
            const info=modelInfoCache[m]||{};
            const TEMPLATE_OPTIONS=[["chatml","ChatML (Qwen / most instruct)"],["llama3","Llama 3.x"],["mistral","Mistral / Mixtral"],["gemma","Gemma 2 / 3"]];
            // Auto-detect template family whenever selected model changes
            const detectedFamily=(()=>{const b=m.toLowerCase();if(b.includes("qwen")||b.includes("chatml"))return"chatml";if(b.includes("llama")||b.includes("hermes"))return"llama3";if(b.includes("mistral")||b.includes("mixtral"))return"mistral";if(b.includes("gemma"))return"gemma";return"chatml";})();
            const effectiveFamily=fixTemplateFamily||detectedFamily;
            const applyTemplate=async()=>{
              const activityId=addActivity({name:`Template patch: ${m}`,kind:"model",message:`Applying ${effectiveFamily} template...`,pct:20});
              setFixingTemplate(true);setFixTemplateMsg(null);
              try{
                const r=await fetch(`${API}/api/models/${encodeURIComponent(m)}/fix-template`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({family:effectiveFamily})});
                const d=await r.json();
                if(!r.ok)throw new Error(d.detail||"Unknown error");
                setFixTemplateMsg({ok:true,text:`Template applied (${effectiveFamily}). Model recreated — tool calling should now work.`});
                updateActivity(activityId,{status:"done",message:"Template applied",pct:100});
                notify({type:"success",text:"Template applied",detail:`${effectiveFamily} template was applied to ${m}.`});
              }catch(e){
                setFixTemplateMsg({ok:false,text:`Failed: ${e.message}`});
                updateActivity(activityId,{status:"error",error:e.message,message:"Template patch failed"});
                notify({type:"error",text:"Template patch failed",detail:e.message});
              }
              setFixingTemplate(false);
            };
            return <div style={{maxWidth:900,animation:"fadeIn .2s"}}>
              {/* Model header */}
              <div style={{...mmPanelStrongS,padding:"18px 20px",marginBottom:14,display:"flex",alignItems:"center",gap:14,flexWrap:"wrap"}}>
                <span style={{fontSize:28,width:40,height:40,borderRadius:8,display:"flex",alignItems:"center",justifyContent:"center",background:`${t.acc}10`,border:`1px solid ${t.acc}22`,flexShrink:0}}>{icon(name)}</span>
                <div style={{flex:1,minWidth:220}}>
                  <div style={mmKickerS}>Installed model</div>
                  <div style={{fontSize:20,fontWeight:900,color:t.text,marginTop:3,wordBreak:"break-word"}}>{name}</div>
                  <div style={{display:"flex",gap:6,flexWrap:"wrap",marginTop:7}}>
                    <span style={mmChipS(sc)}>{tag}</span>
                    {quant&&<span style={mmChipS(quantColor(quant))}>{quant}</span>}
                    {format&&<span style={mmChipS(t.mut,`${t.surface}66`)}>{format}</span>}
                    {hasCustom&&<span style={mmChipS(t.acc)}>Custom overrides</span>}
                  </div>
                </div>
                <div style={{display:"flex",gap:8,alignItems:"center",flexShrink:0}}>
                  <button onClick={()=>useModelFromManager(m)} style={{...btnS(t.acc),padding:"8px 16px",fontSize:12,fontWeight:900}}>Use Model</button>
                  <button onClick={()=>deleteModel(m)} title="Delete model" style={mmIconBtnS(t.err)}><IC.Trash/></button>
                </div>
              </div>
              {/* Tool calling template fix */}
              <details style={{...mmPanelS,marginBottom:14,padding:"0 14px"}}>
                <summary style={{fontSize:12,fontWeight:900,color:t.warm,cursor:"pointer",padding:"12px 0",display:"flex",alignItems:"center",gap:7}}>🔧 Advanced · Tool Calling Template</summary>
                <div style={{fontSize:11,color:t.mut,margin:"0 0 12px",lineHeight:1.5}}>Apply a chat template so this model can use tools. This is mainly for custom GGUF models.</div>
                <div style={{display:"flex",gap:8,alignItems:"center",flexWrap:"wrap",marginBottom:fixTemplateMsg?10:0}}>
                  <select value={effectiveFamily} onChange={e=>setFixTemplateFamily(e.target.value)}
                    style={{background:t.bgDeep,border:`1px solid ${t.brd}44`,color:t.text,padding:"7px 10px",borderRadius:7,fontFamily:font,fontSize:12,flex:1,minWidth:160,outline:"none",cursor:"pointer"}}>
                    {TEMPLATE_OPTIONS.map(([k,l])=><option key={k} value={k}>{l}</option>)}
                  </select>
                  <button onClick={applyTemplate} disabled={fixingTemplate}
                    style={{...btnS(t.warm),padding:"7px 16px",fontSize:12,flexShrink:0,opacity:fixingTemplate?0.6:1}}>
                    {fixingTemplate?"Applying...":"Apply Template"}
                  </button>
                </div>
                {fixTemplateMsg&&<div style={{fontSize:11,color:fixTemplateMsg.ok?t.ok:t.err,padding:"6px 10px",background:fixTemplateMsg.ok?`${t.ok}12`:`${t.err}12`,borderRadius:7,border:`1px solid ${fixTemplateMsg.ok?t.ok:t.err}33`}}>{fixTemplateMsg.text}</div>}
              </details>
              {/* Info strip */}
              {(paramSz||diskSz||family||modifiedAt)&&<div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(130px,1fr))",gap:8,marginBottom:14}}>
                {paramSz&&<div style={{...mmPanelS,padding:"10px 12px"}}><div style={mmKickerS}>Parameters</div><div style={{fontSize:15,fontWeight:900,color:t.text,marginTop:4}}>{paramSz}</div></div>}
                {diskSz&&<div style={{...mmPanelS,padding:"10px 12px"}}><div style={mmKickerS}>Disk Size</div><div style={{fontSize:15,fontWeight:900,color:t.text,marginTop:4}}>{diskSz}</div></div>}
                {family&&<div style={{...mmPanelS,padding:"10px 12px"}}><div style={mmKickerS}>Family</div><div style={{fontSize:15,fontWeight:900,color:t.text,textTransform:"capitalize",marginTop:4}}>{family}</div></div>}
                {modifiedAt&&<div style={{...mmPanelS,padding:"10px 12px"}}><div style={mmKickerS}>Added</div><div style={{fontSize:13,fontWeight:800,color:t.dim,marginTop:4}}>{modifiedAt}</div></div>}
              </div>}
              {/* Modelfile viewer */}
              <div style={{...mmPanelS,marginBottom:14,padding:"12px 14px"}}>
                <button onClick={()=>setShowModelfile(p=>!p)} style={{background:"none",border:"none",color:t.dim,cursor:"pointer",fontSize:12,padding:0,display:"flex",alignItems:"center",gap:6,marginBottom:showModelfile?10:0,fontWeight:800}}>
                  <span style={{transition:"transform .2s",transform:showModelfile?"rotate(90deg)":"none",display:"inline-block"}}>›</span>
                  {modelInfoLoading&&!info.modelfile?"Loading modelfile...":"View Modelfile"}
                </button>
                {showModelfile&&<pre style={{background:t.bgDeep,border:`1px solid ${t.brd}33`,borderRadius:10,padding:"12px 14px",fontSize:11,color:t.dim,overflowX:"auto",whiteSpace:"pre-wrap",wordBreak:"break-all",maxHeight:260,overflowY:"auto",lineHeight:1.5}}>{info.modelfile||"No modelfile data."}</pre>}
              </div>
              {/* Parameter sliders */}
              <div style={{...mmSectionTitleS,marginTop:18}}>Generation overrides</div>
              <div style={{fontSize:12,color:t.mut,marginBottom:12}}>Leave blank to inherit global defaults.</div>
              {[
                ["temperature","Temperature","0","2","0.05","Precise","Balanced","Creative"],
                ["top_p","Top-P","0.1","1","0.05","Focused","Balanced","Diverse"],
                ["repeat_penalty","Repeat Penalty","1","2","0.05","None","Normal","Strong"],
              ].map(([key,lbl,min,max,step,l1,l2,l3])=>{
                const raw=mp[key]||"";const isEmpty=raw===""||raw==="default";
                const numVal=isEmpty?parseFloat(key==="temperature"?defaultTemp:key==="top_p"?defaultTopP:"1.1"):parseFloat(raw);
                return <div key={key} style={{...mmPanelS,padding:"13px 14px",marginBottom:10}}>
                  <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:8}}>
                    <span style={{fontSize:12,color:isEmpty?t.mut:t.dim,textTransform:"uppercase",letterSpacing:.6,fontWeight:900}}>{lbl}</span>
                    <div style={{display:"flex",alignItems:"center",gap:10}}>
                      {!isEmpty&&<button onClick={()=>setModelParams(p=>({...p,[m]:{...(p[m]||{}),[key]:""}}))} style={{background:"none",border:"none",color:t.mut,cursor:"pointer",fontSize:10,padding:0,opacity:.7}}>reset</button>}
                      <span style={{fontSize:15,fontWeight:900,color:isEmpty?t.mut:t.acc,fontVariantNumeric:"tabular-nums",minWidth:54,textAlign:"right"}}>{isEmpty?"global":numVal.toFixed(2)}</span>
                    </div>
                  </div>
                  <input type="range" min={min} max={max} step={step} value={numVal}
                    onChange={e=>setModelParams(p=>({...p,[m]:{...(p[m]||{}),[key]:e.target.value}}))}
                    style={{width:"100%",accentColor:isEmpty?t.mut:t.acc,cursor:"pointer",opacity:isEmpty?0.6:1,height:6}}/>
                  <div style={{display:"flex",justifyContent:"space-between",fontSize:10,color:t.mut,marginTop:4}}><span>{l1}</span><span>{l2}</span><span>{l3}</span></div>
                </div>;
              })}
              {/* Top-K */}
              <div style={{marginBottom:10}}>
                {(()=>{
                  const key="top_k";const val=mp[key]||"";const isEmpty=val===""||val==="default";
                  return <div style={{...mmPanelS,padding:"13px 14px",border:`1px solid ${isEmpty?t.brd:t.acc}22`}}>
                    <div style={{display:"flex",justifyContent:"space-between",marginBottom:8}}>
                      <span style={{fontSize:12,color:t.mut,textTransform:"uppercase",letterSpacing:.6,fontWeight:900}}>Top-K</span>
                      {!isEmpty&&<button onClick={()=>setModelParams(p=>({...p,[m]:{...(p[m]||{}),[key]:""}}))} style={{background:"none",border:"none",color:t.mut,cursor:"pointer",fontSize:10,padding:0}}>reset</button>}
                    </div>
                    <input type="number" value={isEmpty?"":val} onChange={e=>setModelParams(p=>({...p,[m]:{...(p[m]||{}),[key]:e.target.value}}))}
                      placeholder="Default" min="1" max="200" step="1"
                      style={{...inputS,padding:"7px 10px",fontSize:13,color:isEmpty?t.mut:t.acc,width:"100%"}}/>
                    <div style={{fontSize:10,color:t.mut,marginTop:5}}>Integer sample pool</div>
                  </div>;
                })()}
              </div>
              {/* Context Window — button grid, defaults to global numCtx */}
              <div style={{...mmPanelS,padding:"13px 14px",marginBottom:16}}>
                {(()=>{
                  const ctxVal=mp["num_ctx"]||"";const isDefault=ctxVal===""||ctxVal==="default";
                  const effectiveCtx=isDefault?numCtx:parseInt(ctxVal);
                  return <div>
                    <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:8}}>
                      <span style={{fontSize:12,color:t.dim,textTransform:"uppercase",letterSpacing:.6,fontWeight:900}}>Context Window <Tip k="num_ctx" t={t}/></span>
                      <div style={{display:"flex",alignItems:"center",gap:8}}>
                        {!isDefault&&<button onClick={()=>setModelParams(p=>({...p,[m]:{...(p[m]||{}),num_ctx:""}}))} style={{background:"none",border:"none",color:t.mut,cursor:"pointer",fontSize:10,padding:0}}>reset to global</button>}
                        <span style={{fontSize:11,color:isDefault?t.mut:t.acc,fontWeight:600}}>{isDefault?"global default":""}</span>
                      </div>
                    </div>
                    <div style={{display:"flex",flexWrap:"wrap",gap:5}}>
                      {[4096,8192,16384,32768,65536,131072,262144].map(v=><button key={v} onClick={()=>setModelParams(p=>({...p,[m]:{...(p[m]||{}),num_ctx:v===numCtx?"":String(v)}}))} style={{padding:"6px 14px",borderRadius:7,border:`1px solid ${effectiveCtx===v?t.acc:t.brd}44`,background:effectiveCtx===v?`${t.acc}22`:t.bgDeep,color:effectiveCtx===v?t.acc:t.dim,fontFamily:font,fontSize:12,cursor:"pointer",fontWeight:effectiveCtx===v?700:400}}>
                        {v>=1024?`${(v/1024).toFixed(0)}K`:v}
                      </button>)}
                    </div>
                    {effectiveCtx>0&&<div style={{fontSize:11,color:isDefault?t.mut:t.acc,marginTop:4,fontWeight:600}}>{effectiveCtx>=1024?`${(effectiveCtx/1024).toFixed(0)}K`:effectiveCtx} tokens{isDefault?" (from Settings)":""}</div>}
                  </div>;
                })()}
              </div>
              {hasCustom&&<button onClick={()=>setModelParams(p=>{const n={...p};delete n[m];return n;})} style={{...btnS(t.err),fontSize:12,width:"100%",justifyContent:"center",padding:"8px 16px"}}>Reset All to Defaults</button>}
            </div>;
          })()
        }
      </div>
    </div>}

    {/* ── HUGGINGFACE TAB ── */}
    {modelsTab==="hf"&&<div style={{flex:1,display:"flex",overflow:"hidden",background:`${t.bg}22`}}>

      {/* Left: search + installed Hugging Face models */}
      <div style={{width:"clamp(360px,30vw,460px)",minWidth:340,borderRight:`1px solid ${t.brd}20`,display:"flex",flexDirection:"column",overflow:"hidden",background:`${t.bgDeep}66`}}>
        <div style={{padding:"12px 12px 10px",borderBottom:`1px solid ${t.brd}18`,flexShrink:0}}>
          <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",gap:8}}>
            <div>
              <div style={mmKickerS}>Hugging Face</div>
              <div style={{fontSize:13,fontWeight:900,color:t.text,marginTop:3}}>Search GGUF models</div>
            </div>
            <span style={mmChipS(t.warm)}>{hfModelCount} installed</span>
          </div>
        </div>

        <>
        <div style={{padding:"12px 12px 10px",flexShrink:0,borderBottom:`1px solid ${t.brd}16`}}>
          <div style={{display:"flex",gap:6}}>
            <input value={hfSearch} onChange={e=>setHfSearch(e.target.value)} onKeyDown={e=>e.key==="Enter"&&hfDoSearch(hfSearch)}
              placeholder="Search HuggingFace models..." style={{...inputS,flex:1,fontSize:13,padding:"9px 11px",background:t.bgDeep}}/>
            <button onClick={()=>hfDoSearch(hfSearch)} disabled={hfLoading||!hfSearch.trim()} style={{...btnS(t.acc),padding:"7px 10px",flexShrink:0,opacity:(!hfLoading&&hfSearch.trim())?1:.55}}><IC.Search/></button>
          </div>
          <label style={{display:"flex",alignItems:"center",gap:6,marginTop:8,cursor:"pointer",fontSize:11,color:t.mut}}>
            <input type="checkbox" checked={hfGgufOnly} onChange={e=>setHfGgufOnly(e.target.checked)} style={{accentColor:t.acc}}/>
            GGUF only (Ollama-compatible)
          </label>
          {!hfResults.length&&!hfLoading&&<div style={{marginTop:10,fontSize:11,color:t.mut,lineHeight:1.7}}>
            Try: {["Llama 3","Qwen2.5","Mistral","Gemma","Phi-3"].map((s,i)=><span key={s}>
              <span style={{color:t.acc,cursor:"pointer"}} onClick={()=>{setHfSearch(s);hfDoSearch(s);}}>{s}</span>
              {i<4?" · ":""}
            </span>)}
          </div>}
        </div>
        <div style={{flex:1,overflowY:"auto",padding:"10px 12px 14px"}}>
          {hfLoading&&<div style={{display:"flex",justifyContent:"center",padding:24,gap:5,alignItems:"center",color:t.mut,fontSize:11}}>
            <div style={{display:"flex",gap:3}}>{[0,1,2].map(i=><div key={i} style={{width:5,height:5,borderRadius:"50%",background:t.acc,animation:`pulse 1.4s ${i*.16}s infinite`}}/>)}</div>Searching...
          </div>}
          {hfResults.map(m=>{
            const [author,name]=m.id.split("/");const isSel=hfSelected?.id===m.id;
            const hfCaps=(()=>{const b=(m.id||"").toLowerCase();const pt=(m.pipeline_tag||"").toLowerCase();const caps=[];
              if(pt==="feature-extraction"||b.match(/embed/))caps.push({label:"Embed",emoji:"🔢",color:"#9b59b6"});
              if(pt==="image-text-to-text"||pt==="image-to-text"||b.match(/llava|vision|[\-:]vl[\-:$]/))caps.push({label:"Vision",emoji:"👁",color:"#e67e22"});
              if(b.match(/coder|codestral|starcoder|deepseek-coder/))caps.push({label:"Code",emoji:"💻",color:"#2ecc71"});
              if(!b.match(/embed/)&&(pt==="text-generation"||b.match(/qwen|llama|mistral|mixtral|command|hermes|deepseek|phi|gemma/)))caps.push({label:"Tools",emoji:"🔧",color:"#3498db"});
              if(b.match(/mixtral|moe|dbrx|switch|jamba|arctic/))caps.push({label:"MoE",emoji:"🔀",color:"#f39c12"});
              if(b.match(/qwen3(?!\.5)|deepseek-r1|r1-|thinking|reflection|reason/)&&!b.match(/qwen3\.5/))caps.push({label:"Thinking",emoji:"💭",color:"#a78bfa"});
              if(b.match(/abliterated|uncensored|unfiltered|dolphin/))caps.push({label:"Uncensored",emoji:"🔓",color:"#ef4444"});
              if(b.match(/instruct|chat|[\-:]it(?:$|[\-:])/))caps.push({label:"Instruct",emoji:"📝",color:"#64748b"});
              return caps;})();
            return <div key={m.id} onClick={()=>hfSelectModel(m)} style={{padding:"13px 14px",borderRadius:8,marginBottom:7,cursor:"pointer",background:isSel?`${t.acc}14`:`${t.surface}40`,border:`1px solid ${isSel?t.acc:t.brd}${isSel?"55":"18"}`,transition:"all .12s",boxShadow:isSel?`inset 3px 0 0 ${t.acc}`:"none"}}>
              <div style={{fontSize:10,color:t.mut,marginBottom:3,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{author}/</div>
              <div style={{fontSize:14,fontWeight:800,color:isSel?t.acc:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",marginBottom:6}}>{name||m.id}</div>
              <div style={{display:"flex",gap:6,alignItems:"center",flexWrap:"wrap"}}>
                <span style={{fontSize:10,color:t.mut}}>⬇ {m.downloads>999?`${(m.downloads/1000).toFixed(1)}k`:m.downloads}</span>
                <span style={{fontSize:10,color:t.mut}}>♥ {m.likes}</span>
                {hfCaps.slice(0,4).map(c=><span key={c.label} style={{...mmChipS(c.color),fontSize:10,padding:"2px 7px",fontWeight:700}}>{c.emoji} {c.label}</span>)}
              </div>
            </div>;
          })}
          {(()=>{
            const hfModels=models.filter(m=>m.startsWith("hf.co/"));
            if(!hfModels.length)return null;
            return <div style={{marginTop:hfResults.length?16:4,paddingTop:12,borderTop:`1px solid ${t.brd}18`}}>
              <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",gap:8,margin:"0 2px 8px"}}>
                <span style={mmKickerS}>Installed from Hugging Face</span>
                <span style={mmMetaS}>{hfModels.length} model{hfModels.length===1?"":"s"}</span>
              </div>
              {hfModels.map(m=>{
                const md=modelDetails[m]||{};
                const quant=(md.details?.quantization_level||"").toUpperCase();
                const paramSz=md.details?.parameter_size||"";
                const diskSz=md.size?fmtSize(md.size):"";
                const isSel=modelParamsOpen===m;
                const repoPath=m.replace("hf.co/","");
                const repoBase=repoPath.split(":")[0];
                const repoName=repoBase.split("/").pop()||repoBase;
                const caps=capTags(repoName);
                return <div key={m} onClick={()=>{const newSel=isSel?null:m;setModelParamsOpen(newSel);setHfSelected(null);setShowModelfile(false);if(newSel&&!modelInfoCache[newSel]){setModelInfoLoading(true);fetch(`${API}/api/models/${encodeURIComponent(newSel)}/info`).then(r=>r.json()).then(d=>setModelInfoCache(p=>({...p,[newSel]:d}))).catch(()=>{}).finally(()=>setModelInfoLoading(false));}}}
                  style={{padding:"11px 12px",borderRadius:8,marginBottom:7,cursor:"pointer",background:isSel?`${t.acc}14`:`${t.surface}38`,border:`1px solid ${isSel?t.acc:t.brd}${isSel?"55":"18"}`,transition:"all .12s",display:"flex",alignItems:"center",gap:10,boxShadow:isSel?`inset 3px 0 0 ${t.acc}`:"none"}}>
                  <span style={{fontSize:17,flexShrink:0,width:22,textAlign:"center"}}>🤗</span>
                  <div style={{flex:1,minWidth:0}}>
                    <div style={{fontSize:10,color:t.mut,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{repoBase}</div>
                    <div style={{fontSize:13,fontWeight:900,color:isSel?t.acc:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",marginTop:2}}>{repoName}</div>
                    <div style={{display:"flex",gap:4,flexWrap:"wrap",alignItems:"center",marginTop:5}}>
                      {quant&&<span style={{...mmChipS(quantColor(quant)),fontSize:9,padding:"2px 6px"}}>{quant}</span>}
                      {paramSz&&<span style={{fontSize:9,color:t.mut}}>{paramSz}</span>}
                      {diskSz&&<span style={{fontSize:9,color:t.mut}}>{diskSz}</span>}
                      {caps.slice(0,2).map(c=><span key={c.label} style={{...mmChipS(c.color),fontSize:9,padding:"2px 6px",fontWeight:700}}>{c.emoji} {c.label}</span>)}
                    </div>
                  </div>
                  <span style={{fontSize:13,color:isSel?t.acc:t.mut,flexShrink:0,transition:"transform .2s",transform:isSel?"rotate(90deg)":"none"}}>›</span>
                </div>;
              })}
            </div>;
          })()}
        </div>
        </>
      </div>

      {/* Right: detail panel — installed model settings OR search detail */}
      <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
        {modelParamsOpen&&modelParamsOpen.startsWith("hf.co/")&&models.includes(modelParamsOpen)?
          /* installed model settings panel */
          <div style={{flex:1,overflowY:"auto",padding:"24px 28px 40px"}}>
            {(()=>{
              const m=modelParamsOpen;const [mname,mtag="latest"]=m.split(":");const mp=modelParams[m]||{};
              const hasCustom=Object.values(mp).some(v=>v&&v!=="default");
              const mdi=modelDetails[m]||{};const det=mdi.details||{};
              const diskSz=mdi.size?fmtSize(mdi.size):"";
              const quant=(det.quantization_level||"").toUpperCase();
              const paramSz=det.parameter_size||"";
              const family=det.family||"";
              const format=(det.format||"").toUpperCase();
              const modifiedAt=mdi.modified_at?(()=>{try{return new Date(mdi.modified_at).toLocaleDateString();}catch{return mdi.modified_at;}})():"";
              const info=modelInfoCache[m]||{};
              const repoPath=m.replace("hf.co/","");
              const repoName=repoPath.split("/").pop()?.split(":")[0]||m;
              const sc=(tag)=>{const l=(tag||"").toLowerCase();if(l.match(/70b|65b|72b/))return t.err;if(l.match(/30b|32b|34b/))return t.warm;if(l.match(/13b|14b|27b/))return t.acc;if(l.match(/7b|8b/))return t.ok;if(l.match(/1b|3b|4b/))return t.f1;return t.mut;};
              return <div style={{maxWidth:900,animation:"fadeIn .2s"}}>
                <div style={{...mmPanelStrongS,padding:"18px 20px",marginBottom:14,display:"flex",alignItems:"center",gap:14,flexWrap:"wrap"}}>
                  <span style={{fontSize:26,width:40,height:40,borderRadius:8,display:"flex",alignItems:"center",justifyContent:"center",background:`${t.warm}10`,border:`1px solid ${t.warm}22`,flexShrink:0}}>🤗</span>
                  <div style={{flex:1,minWidth:0}}>
                    <div style={mmKickerS}>{repoPath.split(":")[0]}</div>
                    <div style={{fontSize:20,fontWeight:900,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",marginTop:3}}>{repoName}</div>
                    <div style={{display:"flex",gap:6,flexWrap:"wrap",marginTop:7}}>
                      <span style={mmChipS(t.warm)}>HuggingFace</span>
                      {quant&&<span style={mmChipS(quantColor(quant))}>{quant}</span>}
                      {format&&<span style={mmChipS(t.mut,`${t.surface}66`)}>{format}</span>}
                      {hasCustom&&<span style={mmChipS(t.acc)}>Custom overrides</span>}
                    </div>
                  </div>
                  <div style={{display:"flex",flexDirection:"column",gap:7,alignItems:"flex-end",flexShrink:0}}>
                    <div style={{display:"flex",gap:6}}>
                      <button onClick={()=>useModelFromManager(m)} style={{...btnS(t.acc),padding:"8px 16px",fontSize:12,fontWeight:900}}>Use Model</button>
                      <button onClick={()=>deleteModel(m)} title="Delete model" style={mmIconBtnS(t.err)}><IC.Trash/></button>
                    </div>
                    <button onClick={e=>{e.stopPropagation();patchToolModel(m,"This model can now use tools.");}} disabled={!!makingToolModel} style={{background:makingToolModel===m?`${t.mut}15`:`${t.ok}18`,border:`1px solid ${makingToolModel===m?t.mut:t.ok}44`,color:makingToolModel===m?t.mut:t.ok,cursor:makingToolModel?"wait":"pointer",padding:"6px 12px",borderRadius:7,fontSize:10,fontWeight:900,whiteSpace:"nowrap",letterSpacing:.2,transition:"all .15s"}}>{makingToolModel===m?"Patching...":"Enable Tool Calling"}</button>
                  </div>
                </div>
                {(paramSz||diskSz||family||modifiedAt)&&<div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(130px,1fr))",gap:8,marginBottom:14}}>
                  {paramSz&&<div style={{...mmPanelS,padding:"10px 12px"}}><div style={mmKickerS}>Parameters</div><div style={{fontSize:15,fontWeight:900,color:t.text,marginTop:4}}>{paramSz}</div></div>}
                  {diskSz&&<div style={{...mmPanelS,padding:"10px 12px"}}><div style={mmKickerS}>Disk Size</div><div style={{fontSize:15,fontWeight:900,color:t.text,marginTop:4}}>{diskSz}</div></div>}
                  {family&&<div style={{...mmPanelS,padding:"10px 12px"}}><div style={mmKickerS}>Family</div><div style={{fontSize:15,fontWeight:900,color:t.text,textTransform:"capitalize",marginTop:4}}>{family}</div></div>}
                  {modifiedAt&&<div style={{...mmPanelS,padding:"10px 12px"}}><div style={mmKickerS}>Added</div><div style={{fontSize:13,fontWeight:800,color:t.dim,marginTop:4}}>{modifiedAt}</div></div>}
                </div>}
                <div style={{...mmPanelS,marginBottom:14,padding:"12px 14px"}}>
                  <button onClick={()=>setShowModelfile(p=>!p)} style={{background:"none",border:"none",color:t.dim,cursor:"pointer",fontSize:12,padding:0,display:"flex",alignItems:"center",gap:6,marginBottom:showModelfile?8:0,fontWeight:800}}>
                    <span style={{transition:"transform .2s",transform:showModelfile?"rotate(90deg)":"none",display:"inline-block"}}>›</span>
                    {modelInfoLoading&&!info.modelfile?"Loading modelfile...":"View Modelfile"}
                  </button>
                  {showModelfile&&<pre style={{background:t.bgDeep,border:`1px solid ${t.brd}33`,borderRadius:8,padding:"10px 12px",fontSize:10,color:t.dim,overflowX:"auto",whiteSpace:"pre-wrap",wordBreak:"break-all",maxHeight:200,overflowY:"auto",lineHeight:1.5}}>{info.modelfile||"No modelfile data."}</pre>}
                </div>
                <div style={{...mmSectionTitleS,marginTop:18}}>Generation overrides</div>
                <div style={{fontSize:12,color:t.mut,marginBottom:12}}>Leave blank to inherit global defaults.</div>
                {[["temperature","Temperature","0","2","0.05","Precise","Balanced","Creative"],["top_p","Top-P","0.1","1","0.05","Focused","Balanced","Diverse"],["repeat_penalty","Repeat Penalty","1","2","0.05","None","Normal","Strong"]].map(([key,lbl,min,max,step,l1,l2,l3])=>{
                  const raw=mp[key]||"";const isEmpty=raw===""||raw==="default";
                  const numVal=isEmpty?parseFloat(key==="temperature"?defaultTemp:key==="top_p"?defaultTopP:"1.1"):parseFloat(raw);
                  return <div key={key} style={{...mmPanelS,padding:"13px 14px",marginBottom:10}}>
                    <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:6}}>
                      <span style={{fontSize:12,color:isEmpty?t.mut:t.dim,textTransform:"uppercase",letterSpacing:.6,fontWeight:900}}>{lbl}</span>
                      <div style={{display:"flex",alignItems:"center",gap:8}}>
                        {!isEmpty&&<button onClick={()=>setModelParams(p=>({...p,[m]:{...(p[m]||{}),[key]:""}}))} style={{background:"none",border:"none",color:t.mut,cursor:"pointer",fontSize:9,padding:0,opacity:.7}}>reset</button>}
                        <span style={{fontSize:15,fontWeight:900,color:isEmpty?t.mut:t.acc,fontVariantNumeric:"tabular-nums",minWidth:54,textAlign:"right"}}>{isEmpty?"global":numVal.toFixed(2)}</span>
                      </div>
                    </div>
                    <input type="range" min={min} max={max} step={step} value={numVal} onChange={e=>setModelParams(p=>({...p,[m]:{...(p[m]||{}),[key]:e.target.value}}))} style={{width:"100%",accentColor:isEmpty?t.mut:t.acc,cursor:"pointer",opacity:isEmpty?0.6:1}}/>
                    <div style={{display:"flex",justifyContent:"space-between",fontSize:9,color:t.mut,marginTop:2}}><span>{l1}</span><span>{l2}</span><span>{l3}</span></div>
                  </div>;
                })}
                <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:14,marginBottom:20}}>
                  {[["top_k","Top-K","1","200","1","Integer sample pool"],["num_ctx","Context (tokens)","2048","131072","1024","Max token window"]].map(([key,lbl,min,max,step,desc])=>{
                    const val=mp[key]||"";const isEmpty=val===""||val==="default";
                    return <div key={key} style={{...mmPanelS,padding:"12px 14px",border:`1px solid ${isEmpty?t.brd:t.acc}22`}}>
                      <div style={{display:"flex",justifyContent:"space-between",marginBottom:6}}>
                        <span style={{fontSize:10,color:t.mut,textTransform:"uppercase",letterSpacing:.6,fontWeight:900}}>{lbl}</span>
                        {!isEmpty&&<button onClick={()=>setModelParams(p=>({...p,[m]:{...(p[m]||{}),[key]:""}}))} style={{background:"none",border:"none",color:t.mut,cursor:"pointer",fontSize:9,padding:0}}>reset</button>}
                      </div>
                      <input type="number" value={isEmpty?"":val} onChange={e=>setModelParams(p=>({...p,[m]:{...(p[m]||{}),[key]:e.target.value}}))} placeholder="Default" min={min} max={max} step={step} style={{...inputS,padding:"5px 8px",fontSize:12,color:isEmpty?t.mut:t.acc,width:"100%"}}/>
                      <div style={{fontSize:9,color:t.mut,marginTop:4}}>{desc}</div>
                    </div>;
                  })}
                </div>
                {hasCustom&&<button onClick={()=>setModelParams(p=>{const n={...p};delete n[m];return n;})} style={{...btnS(t.err),fontSize:10,width:"100%",justifyContent:"center"}}>Reset All to Defaults</button>}
              </div>;
            })()}
          </div>
        :!hfSelected?<div style={{flex:1,display:"flex",alignItems:"center",justifyContent:"center",padding:24}}>
          <div style={{...mmPanelS,padding:"28px 34px",textAlign:"center",maxWidth:380}}>
            <span style={{fontSize:32}}>🤗</span>
            <div style={{fontSize:14,fontWeight:900,color:t.text,marginTop:10}}>Search HuggingFace</div>
            <div style={{fontSize:12,color:t.mut,marginTop:5,lineHeight:1.5}}>Select a GGUF model to inspect files, compatibility, and README details.</div>
          </div>
        </div>:
        <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
          {/* Model header */}
          <div style={{padding:"16px 20px",flexShrink:0,borderBottom:`1px solid ${t.brd}20`,display:"flex",alignItems:"center",gap:12,background:`${t.surface}33`}}>
            <span style={{fontSize:24,width:36,height:36,borderRadius:8,display:"flex",alignItems:"center",justifyContent:"center",background:`${t.warm}10`,border:`1px solid ${t.warm}22`,flexShrink:0}}>🤗</span>
            <div style={{flex:1,minWidth:0}}>
              <div style={mmKickerS}>{hfSelected.id.split("/")[0]}/</div>
              <div style={{fontSize:17,fontWeight:900,color:t.acc,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",marginTop:3}}>{hfSelected.id.split("/")[1]||hfSelected.id}</div>
            </div>
            <div style={{display:"flex",gap:12,fontSize:10,color:t.mut,flexShrink:0,alignItems:"center"}}>
              {hfModelInfo?.gguf_files?.length>0&&(()=>{
                const total=hfModelInfo.gguf_files.reduce((a,f)=>a+(f.size||0),0);
                return total>0?<span style={{fontWeight:700,color:t.warm,fontSize:11}}>{fmtSize(total)}</span>:null;
              })()}
              <span>⬇ {hfSelected.downloads>999?`${(hfSelected.downloads/1000).toFixed(1)}k`:hfSelected.downloads}</span>
              <span>♥ {hfSelected.likes}</span>
            </div>
          </div>
          {hfModelInfo&&(()=>{
            const compat=hfCompatibilityReport(hfSelected,hfModelInfo,hfReadme,hfSelectedFiles);
            const c=compat.level==="bad"?t.err:compat.level==="warn"?(t.warm||"#eab308"):t.ok;
            const icon=compat.level==="bad"?"⚠":compat.level==="warn"?"△":"✓";
            const title=compat.level==="ok"?"No obvious Ollama compatibility flags":compat.level==="bad"?"Likely compatibility issue":"Possible compatibility issue";
            return <div style={{padding:"10px 16px",flexShrink:0,borderBottom:`1px solid ${c}22`,background:`${c}0C`,display:"flex",gap:10,alignItems:"flex-start"}}>
              <span style={{color:c,fontSize:13,fontWeight:900,lineHeight:"18px",flexShrink:0}}>{icon}</span>
              <div style={{flex:1,minWidth:0}}>
                <div style={{fontSize:11,fontWeight:800,color:c,letterSpacing:.2}}>Ollama Compatibility · {title}</div>
                <div style={{display:"flex",gap:6,flexWrap:"wrap",marginTop:5}}>
                  {compat.issues.length?compat.issues.map(issue=>{
                    const ic=issue.level==="bad"?t.err:issue.level==="warn"?(t.warm||"#eab308"):t.f1;
                    return <span key={issue.label} title={issue.detail} style={{fontSize:9,color:ic,border:`1px solid ${ic}44`,background:`${ic}12`,borderRadius:6,padding:"2px 6px",fontWeight:700}}>{issue.label}</span>;
                  }):<span style={{fontSize:10,color:t.mut}}>Metadata and README do not show common Ollama loader risks.</span>}
                </div>
              </div>
            </div>;
          })()}
          {/* Download bar — sticky top */}
          {hfSelectedFiles.length>0&&(()=>{
            const compat=hfCompatibilityReport(hfSelected,hfModelInfo,hfReadme,hfSelectedFiles);
            const risky=compat.level!=="ok";
            const c=compat.level==="bad"?t.err:compat.level==="warn"?(t.warm||"#eab308"):t.ok;
            return <div style={{padding:"10px 16px",flexShrink:0,borderBottom:`1px solid ${t.brd}18`,background:`${c}06`,display:"flex",alignItems:"center",gap:8,flexWrap:"wrap"}}>
              <input value={hfDownloadName} onChange={e=>setHfDownloadName(e.target.value)} placeholder="Model name, e.g. llama3-q5" style={{...inputS,fontSize:11,padding:"7px 10px",width:220,flex:"none",background:t.bgDeep}}/>
              <span style={{fontSize:9,color:t.mut,flexShrink:0}}>{hfSelectedFiles.length} file{hfSelectedFiles.length>1?"s":""} · {fmtSize((hfModelInfo?.gguf_files||[]).filter(f=>hfSelectedFiles.includes(f.name)).reduce((a,f)=>a+(f.size||0),0))}</span>
              {risky&&<span style={{fontSize:9,color:c,fontWeight:800,flexShrink:0}}>compatibility flagged</span>}
              <div style={{flex:1}}/>
              <button onClick={hfDownload} disabled={hfDownloading||!hfDownloadName.trim()} style={{...btnS(hfDownloading?t.mut:c),padding:"6px 14px",fontSize:11,flexShrink:0,opacity:hfDownloading?0.7:1}}>
                <IC.Download/>{hfDownloading?"Queued":risky?"Download anyway":"Download"}
              </button>
            </div>;
          })()}
          {/* Body: files left, readme right */}
          <div style={{flex:1,display:"flex",overflow:"hidden"}}>
            {/* Files + download */}
            <div style={{width:"clamp(280px,24vw,320px)",minWidth:280,borderRight:`1px solid ${t.brd}18`,overflowY:"auto",padding:"16px 14px",background:`${t.bgDeep}33`}}>
              <div style={{...mmKickerS,marginBottom:10}}>GGUF Files</div>
              {!hfModelInfo?<div style={{color:t.mut,fontSize:11,fontStyle:"italic"}}>Loading...</div>:
              hfModelInfo.gguf_files.length===0?<div style={{color:t.mut,fontSize:11,fontStyle:"italic"}}>No GGUF files.</div>:
              <>
                {(()=>{
                  const groups=getPartGroups(hfModelInfo.gguf_files);
                  return Object.entries(groups).map(([groupKey,files])=>{
                    const isGroup=files.length>1;
                    const totalSize=files.reduce((a,f)=>a+f.size,0);
                    const allSel=files.every(f=>hfSelectedFiles.includes(f.name));
                    const toggle=()=>{if(allSel)setHfSelectedFiles(p=>p.filter(n=>!files.map(f=>f.name).includes(n)));else setHfSelectedFiles(p=>[...new Set([...p,...files.map(f=>f.name)])]);};
                    const fname=isGroup?groupKey:files[0].name;
                    const ql=quantLabel(fname);const qc=quantColor(ql);
                    return <div key={groupKey} onClick={toggle} style={{display:"flex",alignItems:"flex-start",gap:8,padding:"9px 10px",borderRadius:8,background:allSel?`${t.acc}14`:`${t.surface}40`,border:`1px solid ${allSel?t.acc:t.brd}${allSel?"55":"18"}`,marginBottom:5,cursor:"pointer",transition:"all .12s"}}>
                      <input type="checkbox" checked={allSel} onChange={toggle} onClick={e=>e.stopPropagation()} style={{accentColor:t.acc,flexShrink:0,marginTop:2}}/>
                      <div style={{flex:1,minWidth:0}}>
                        <div style={{display:"flex",alignItems:"center",gap:5,marginBottom:2,flexWrap:"wrap"}}>
                          {ql&&<span style={{fontSize:9,padding:"1px 6px",borderRadius:5,background:`${qc}20`,color:qc,fontWeight:700,border:`1px solid ${qc}44`,letterSpacing:.3,flexShrink:0}}>{ql}</span>}
                          <span style={{fontSize:10,fontWeight:600,color:allSel?t.acc:t.text,wordBreak:"break-all",lineHeight:1.3}}>{fname}</span>
                        </div>
                        <div style={{fontSize:9,color:t.mut}}>{isGroup?`${files.length} parts · ${fmtSize(totalSize)}`:fmtSize(files[0].size)}</div>
                      </div>
                    </div>;
                  });
                })()}
              </>}
            </div>
            {/* README */}
            <div style={{flex:1,overflowY:"auto",padding:"18px 22px"}}>
              <div style={{...mmKickerS,marginBottom:12}}>README</div>
              {hfReadmeLoading?<div style={{color:t.mut,fontSize:11,fontStyle:"italic"}}>Loading README...</div>:
              hfReadme?<div style={{fontSize:12,lineHeight:1.7,color:t.dim}}><MDWrap>{md(hfReadme)}</MDWrap></div>:
              <div style={{color:t.mut,fontSize:11,fontStyle:"italic"}}>No README available.</div>}
            </div>
          </div>
        </div>}
      </div>
    </div>}

    {/* ── HYPRFIT TAB ── */}
    {modelsTab==="hyprfit"&&<div style={{flex:1,display:"flex",overflow:"hidden",background:`${t.bg}22`}}>
      <div style={{width:"clamp(340px,28vw,440px)",minWidth:320,borderRight:`1px solid ${t.brd}20`,background:`${t.bgDeep}66`,overflowY:"auto",padding:"18px 18px 28px",boxSizing:"border-box"}}>
        <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",gap:10,marginBottom:14}}>
          <div>
            <div style={mmKickerS}>HyprFit</div>
            <div style={{fontSize:18,fontWeight:900,color:t.text,marginTop:4}}>Hardware profile</div>
            <div style={{fontSize:12,color:t.mut,marginTop:5,lineHeight:1.45}}>Saved profile used for model fit and loadout estimates.</div>
          </div>
          <div style={{display:"flex",gap:6}}>
            <button title="Refresh recommendations" onClick={()=>loadHyprfit(true)} disabled={hyprfitLoading} style={{...mmIconBtnS(t.acc),opacity:hyprfitLoading?0.6:1}}><IC.Refresh/></button>
            <button title="Rescan hardware" onClick={rescanHyprfitHardware} disabled={hyprfitRescanning} style={{...mmIconBtnS(t.warm),opacity:hyprfitRescanning?0.6:1}}><IC.Activity/></button>
          </div>
        </div>
        {!hyprfitProfile?<div style={{...mmPanelS,padding:20,color:t.mut,fontSize:12}}>Loading profile...</div>:<div style={{...mmPanelStrongS,padding:16}}>
          {[
            ["name","Profile name","text"],
            ["gpu_name","GPU label","text"],
          ].map(([key,label,type])=><label key={key} style={{display:"block",marginBottom:11}}>
            <span style={{display:"block",fontSize:10,color:t.mut,textTransform:"uppercase",letterSpacing:.6,fontWeight:800,marginBottom:4}}>{label}</span>
            <input type={type} value={hyprfitProfile[key]||""} onChange={e=>setHyprfitProfile(p=>({...p,[key]:e.target.value}))} style={{...inputS,fontSize:12,padding:"8px 10px",background:t.bgDeep}}/>
          </label>)}
          <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:10}}>
            {[
              ["gpu_count","GPUs",1,16,1],
              ["total_vram_gb","VRAM GB",0,512,1],
              ["system_ram_gb","RAM GB",1,2048,1],
              ["max_loaded_models","Warm models",1,32,1],
              ["num_parallel","Parallel slots",1,16,1],
            ].map(([key,label,min,max,step])=><label key={key} style={{display:"block",marginBottom:10}}>
              <span style={{display:"block",fontSize:10,color:t.mut,textTransform:"uppercase",letterSpacing:.6,fontWeight:800,marginBottom:4}}>{label}</span>
              <input type="number" min={min} max={max} step={step} value={hyprfitProfile[key]??""} onChange={e=>setHyprfitProfile(p=>({...p,[key]:Number(e.target.value)}))} style={{...inputS,fontSize:12,padding:"8px 10px",background:t.bgDeep}}/>
            </label>)}
            <label style={{display:"block",marginBottom:10}}>
              <span style={{display:"block",fontSize:10,color:t.mut,textTransform:"uppercase",letterSpacing:.6,fontWeight:800,marginBottom:4}}>KV cache</span>
              <select value={hyprfitProfile.kv_cache_type||"q8_0"} onChange={e=>setHyprfitProfile(p=>({...p,kv_cache_type:e.target.value}))} style={{...inputS,fontSize:12,padding:"8px 10px",background:t.bgDeep}}>
                <option value="q8_0">q8_0</option>
                <option value="f16">f16</option>
                <option value="q4_0">q4_0</option>
              </select>
            </label>
          </div>
          <label style={{display:"flex",alignItems:"center",gap:8,fontSize:11,color:t.dim,cursor:"pointer",margin:"2px 0 14px"}}>
            <input type="checkbox" checked={!!hyprfitProfile.sched_spread} onChange={e=>setHyprfitProfile(p=>({...p,sched_spread:e.target.checked}))} style={{accentColor:t.acc}}/>
            Spread model across visible GPUs
          </label>
          <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8}}>
            <button onClick={saveHyprfitProfile} disabled={hyprfitSaving} style={{...btnS(t.ok),justifyContent:"center",padding:"9px 10px",fontSize:12,opacity:hyprfitSaving?0.65:1}}>{hyprfitSaving?"Saving...":"Save Profile"}</button>
            <button onClick={rescanHyprfitHardware} disabled={hyprfitRescanning} style={{...btnS(t.warm),justifyContent:"center",padding:"9px 10px",fontSize:12,opacity:hyprfitRescanning?0.65:1}}><IC.Activity/> {hyprfitRescanning?"Scanning":"Rescan Hardware"}</button>
          </div>
          <div style={{display:"flex",gap:6,flexWrap:"wrap",marginTop:12}}>
            <span style={mmChipS(t.mut,`${t.surface}66`)}>{hyprfitProfile.backend||"unknown"}</span>
            <span style={mmChipS(t.mut,`${t.surface}66`)}>{hyprfitProfile.unified_memory?"unified memory":"discrete memory"}</span>
            <span style={mmChipS(t.mut,`${t.surface}66`)}>{hyprfitProfile.source||"manual"}</span>
            {hyprfitRescanStatus?.detection_mode&&(()=>{
              const copy=hyprfitRescanCopy(hyprfitRescanStatus);
              return <>
                <span style={mmChipS(copy.color)}>{copy.chip}</span>
                {hyprfitRescanStatus.detection_mode==="remote_ssh_detector"&&hyprfitRescanStatus.detected_profile?.backend&&<span style={mmChipS(t.ok)}>{hyprfitRescanStatus.detected_profile.backend}</span>}
                {hyprfitRescanStatus.target==="ollama"&&<span style={mmChipS(hyprfitRescanStatus.ollama_reachable?t.ok:t.warm)}>{hyprfitRescanStatus.ollama_reachable?"Ollama reachable":"Ollama unreachable"}</span>}
                {hyprfitRescanStatus.target==="ollama"&&hyprfitRescanStatus.ollama_url&&<span title={hyprfitRescanStatus.ollama_url} style={{...mmChipS(t.mut,`${t.surface}66`),maxWidth:180,overflow:"hidden",textOverflow:"ellipsis"}}>{hyprfitRescanStatus.ollama_url.replace(/^https?:\/\//,"")}</span>}
              </>;
            })()}
          </div>
        </div>}
        {hyprfit?.assumptions?.length>0&&<div style={{...mmPanelS,padding:"12px 14px",marginTop:14}}>
          <div style={{...mmKickerS,marginBottom:7}}>Estimate notes</div>
          {hyprfit.assumptions.map((a,i)=><div key={i} style={{fontSize:11,color:t.mut,lineHeight:1.5,marginBottom:i===hyprfit.assumptions.length-1?0:5}}>{a}</div>)}
        </div>}
      </div>
      <div style={{flex:1,overflowY:"auto",padding:"22px 28px 38px"}}>
        {(()=>{
          const fallbackCats=[
            {id:"for_you",label:"For You",summary:"Best fit for this hardware",count:(hyprfit?.recommendations||[]).length},
            {id:"popular",label:"Popular",count:0},
            {id:"newest",label:"Newest",count:0},
            {id:"coding",label:"Coding",count:0},
            {id:"chat",label:"Chat",count:0},
            {id:"tool_calling",label:"Tool Calling",count:0},
            {id:"reasoning",label:"Reasoning",count:0},
            {id:"moe",label:"MoE",count:0},
            {id:"long_context",label:"Long Context",count:0},
            {id:"small_fast",label:"Small/Fast",count:0},
            {id:"vision",label:"Vision",count:0},
            {id:"embeddings",label:"Embeddings",count:0},
          ];
          const cats=hyprfit?.categories?.length?hyprfit.categories:fallbackCats;
          const activeCat=cats.some(c=>c.id===hyprfitCategory)?hyprfitCategory:"for_you";
          const activeMeta=cats.find(c=>c.id===activeCat)||cats[0]||fallbackCats[0];
          const grouped=hyprfit?.grouped_recommendations||{};
          const recs=grouped[activeCat]||((activeCat==="for_you")?(hyprfit?.recommendations||[]):[]);
          const compact=n=>{const v=Number(n||0);if(v>=1000000)return`${(v/1000000).toFixed(v>=10000000?0:1)}m`;if(v>=1000)return`${(v/1000).toFixed(v>=10000?0:1)}k`;return String(v);};
          const fmtDate=v=>{if(!v)return"";try{return new Date(v).toLocaleDateString();}catch{return v;}};
          return <div style={{maxWidth:1120}}>
            <div style={{display:"flex",alignItems:"flex-start",justifyContent:"space-between",gap:14,marginBottom:14,flexWrap:"wrap"}}>
              <div>
                <div style={mmKickerS}>Model loadout</div>
                <div style={{fontSize:22,fontWeight:900,color:t.text,marginTop:4}}>HyprFit recommendations</div>
                <div style={{fontSize:12,color:t.mut,marginTop:5,lineHeight:1.5,maxWidth:660}}>{activeMeta?.summary||"Ranked for the saved hardware profile, current Ollama inventory, warm-model budget, and estimated context memory."}</div>
              </div>
              <div style={{display:"flex",gap:8,flexWrap:"wrap",justifyContent:"flex-end"}}>
                <span style={mmChipS(t.acc)}>{hyprfit?.profile?.total_vram_gb??0} GB VRAM</span>
                <span style={mmChipS(t.mut,`${t.surface}66`)}>{hyprfit?.system?.backend||hyprfit?.profile?.backend||"backend"}</span>
                <span style={mmChipS(t.warm)}>{hyprfit?.profile?.kv_cache_type||"q8_0"} KV</span>
                <span style={mmChipS(t.f1)}>{hyprfit?.profile?.max_loaded_models||1} warm</span>
                {hyprfit?.system?.gguf_budget?.solo_gb!=null&&<span style={mmChipS(t.mut,`${t.surface}66`)}>{hyprfit.system.gguf_budget.solo_gb} GB solo GGUF</span>}
                {hyprfit?.live_enabled&&<span style={mmChipS(t.mut,`${t.surface}66`)}>{hyprfit?.live_count||0} live</span>}
              </div>
            </div>
            <div style={{display:"flex",gap:7,flexWrap:"wrap",marginBottom:16}}>
              {cats.map(c=>{
                const active=c.id===activeCat;
                const count=c.count??(grouped[c.id]||[]).length;
                return <button key={c.id} onClick={()=>setHyprfitCategory(c.id)} style={{...btnS(active?t.acc:t.mut,active?`${t.acc}1A`:`${t.surface}44`),padding:"7px 10px",fontSize:10,borderColor:active?`${t.acc}66`:`${t.brd}22`,whiteSpace:"nowrap"}}>
                  <span>{c.label}</span>
                  <span style={{...mmChipS(active?t.acc:t.mut,active?`${t.acc}12`:`${t.surface}66`),fontSize:8,padding:"1px 5px"}}>{count}</span>
                </button>;
              })}
            </div>
            {hyprfitLoading&&!hyprfit?<div style={{...mmPanelS,padding:30,color:t.mut,fontSize:12,textAlign:"center"}}>Loading HyprFit recommendations...</div>:
            hyprfit?.ollama_error?<div style={{...mmPanelS,padding:"11px 13px",marginBottom:10,borderColor:`${t.warm}44`,color:t.warm,fontSize:11}}>Ollama inventory unavailable: {hyprfit.ollama_error}</div>:null}
            {hyprfit?.hf_error&&<div style={{...mmPanelS,padding:"11px 13px",marginBottom:10,borderColor:`${t.warm}33`,color:t.mut,fontSize:11}}>Hugging Face discovery unavailable: {hyprfit.hf_error}</div>}
            <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(292px,1fr))",gap:12}}>
              {recs.map(rec=>{
                const fitColor=rec.fit==="great"?t.ok:rec.fit==="fits"?t.acc:rec.fit==="tight"?t.warm:rec.fit==="too_large"?t.err:t.mut;
                const sourceColor=rec.source==="Hugging Face"?t.warm:rec.source==="Curated + HF"?t.f1:t.acc;
                const modified=fmtDate(rec.lastModified||rec.createdAt);
                const paramsLabel=`${rec.params_estimated?"~":""}${rec.params_b}B`;
                const scorePct=Math.round((rec.score||0)*100);
                const requiredGb=rec.required_gb??rec.estimated_vram_gb;
                return <div key={`${activeCat}-${rec.id}`} style={{...mmPanelStrongS,padding:16,display:"flex",flexDirection:"column",gap:12,minHeight:292,borderColor:`${fitColor}33`}}>
                  <div style={{display:"flex",alignItems:"flex-start",justifyContent:"space-between",gap:10}}>
                    <div style={{minWidth:0}}>
                      <div style={mmKickerS}>{rec.purpose}</div>
                      <div style={{fontSize:17,fontWeight:900,color:t.text,marginTop:4,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{rec.name}</div>
                      <div style={{fontSize:11,color:t.mut,marginTop:4,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{rec.pull_name}</div>
                    </div>
                    <span style={mmChipS(fitColor)}>{rec.fit_label}</span>
                  </div>
                  <div style={{fontSize:12,color:t.dim,lineHeight:1.5,minHeight:54}}>{rec.summary}</div>
                  <div style={{display:"grid",gridTemplateColumns:"repeat(4,minmax(0,1fr))",gap:8}}>
                    <div style={{...mmPanelS,padding:"9px 10px"}}><div style={mmKickerS}>Score</div><div style={{fontSize:15,fontWeight:900,color:t.text,marginTop:4}}>{scorePct}</div></div>
                    <div style={{...mmPanelS,padding:"9px 10px"}}><div style={mmKickerS}>Need</div><div style={{fontSize:15,fontWeight:900,color:fitColor,marginTop:4}}>{requiredGb} GB</div></div>
                    <div style={{...mmPanelS,padding:"9px 10px"}}><div style={mmKickerS}>Context</div><div style={{fontSize:15,fontWeight:900,color:t.text,marginTop:4}}>{formatModelCtx(rec.context_tokens)}</div></div>
                    <div style={{...mmPanelS,padding:"9px 10px"}}><div style={mmKickerS}>Speed</div><div style={{fontSize:12,fontWeight:900,color:t.text,marginTop:5,lineHeight:1.2}}>{rec.estimated_speed}</div></div>
                  </div>
                  <div style={{display:"flex",gap:6,flexWrap:"wrap",alignItems:"center"}}>
                    <span style={mmChipS(sourceColor)}>{rec.source||"Curated"}</span>
                    <span style={mmChipS(t.mut,`${t.surface}66`)}>{paramsLabel}</span>
                    <span style={mmChipS(quantColor(rec.quant))}>{rec.quant}</span>
                    <span style={mmChipS(t.mut,`${t.surface}66`)}>{rec.format||"gguf"}</span>
                    <span style={mmChipS(t.mut,`${t.surface}66`)}>{(rec.run_mode||"runtime").replaceAll("_"," ")}</span>
                    {rec.installed&&<span style={mmChipS(t.ok)}>Installed</span>}
                    {(rec.badges||[]).slice(0,3).map(b=><span key={b} style={mmChipS(t.mut,`${t.surface}66`)}>{b}</span>)}
                  </div>
                  {(rec.downloads||rec.likes||modified)&&<div style={{display:"flex",gap:10,flexWrap:"wrap",fontSize:10,color:t.mut}}>
                    {rec.downloads? <span>⬇ {compact(rec.downloads)}</span>:null}
                    {rec.likes? <span>♥ {compact(rec.likes)}</span>:null}
                    {modified? <span>{modified}</span>:null}
                  </div>}
                  <div style={{fontSize:11,color:fitColor,lineHeight:1.45,marginTop:"auto"}}>{rec.fit_note}</div>
                  <div style={{display:"flex",gap:8,flexWrap:"wrap"}}>
                    <button onClick={()=>fillPullFromHyprfit(rec.pull_name)} style={{...btnS(t.acc),padding:"7px 11px",fontSize:11}}><IC.Download/> Fill Pull Field</button>
                    {rec.installed&&<button onClick={()=>useModelFromManager(rec.installed_name)} style={{...btnS(t.ok),padding:"7px 11px",fontSize:11}}>Use Installed</button>}
                  </div>
                </div>;
              })}
            </div>
            {!hyprfitLoading&&!recs.length&&<div style={{...mmPanelS,padding:30,color:t.mut,fontSize:12,textAlign:"center"}}>No recommendations available in {activeMeta?.label||"this category"}.</div>}
          </div>;
        })()}
      </div>
    </div>}
  </div>

      :panel==="analytics"?<AnalyticsPanel t={t} btnS={btnS} cardS={cardS} analyticsDays={analyticsDays} setAnalyticsDays={setAnalyticsDays} analyticsGroup={analyticsGroup} setAnalyticsGroup={setAnalyticsGroup} loadAnalytics={loadAnalytics} analyticsData={analyticsData}/>

      :panel==="settings"?ReactDOM.createPortal(<div style={{position:"fixed",inset:0,zIndex:100,background:"rgba(0,0,0,.7)",display:"flex",alignItems:"center",justifyContent:"center",backdropFilter:"blur(6px)",fontFamily:font,color:t.text}} onClick={e=>{if(e.target===e.currentTarget)closeSettings();}}>
    <div style={{width:"min(1100px,95vw)",maxHeight:"85vh",height:"85vh",display:"grid",gridTemplateColumns:"260px minmax(0,1fr)",background:t.bgDeep,border:`1px solid ${t.brd}44`,borderRadius:16,boxShadow:"0 8px 48px #0008",overflow:"hidden",animation:"fadeIn .25s"}}>
      <div style={{borderRight:`1px solid ${t.brd}44`,background:`${t.surface}50`,padding:18,display:"flex",flexDirection:"column",gap:8,overflow:"hidden"}}>
        <div style={{display:"flex",alignItems:"center",gap:9,marginBottom:10}}>
          <span style={{color:t.acc,display:"flex"}}><IC.Settings/></span>
          <div>
            <div style={{fontSize:15,fontWeight:800,color:t.text,letterSpacing:.4}}>Settings</div>
          </div>
        </div>
        <div style={{flex:1,minHeight:0,overflowY:"auto",display:"flex",flexDirection:"column",gap:8,paddingRight:2}}>
          {settingsTabs.filter(([id])=>id!=="changelog").map(([id,icon,label,hint])=>{const active=settingsTab===id;return <button key={id} onClick={()=>{setSettingsTab(id);if(id!=="connections")setShowHealthMonitor(false);}} style={{width:"100%",display:"flex",alignItems:"center",gap:10,textAlign:"left",padding:"10px 11px",borderRadius:10,border:`1px solid ${active?t.acc:t.brd}33`,background:active?`${t.acc}18`:`${t.bgDeep}88`,color:active?t.acc:t.dim,cursor:"pointer",fontFamily:font,boxShadow:"none"}}>
            <span style={{fontSize:16,width:20,textAlign:"center",flexShrink:0}}>{icon}</span>
            <span style={{minWidth:0,flex:1}}>
              <span style={{display:"block",fontSize:12,fontWeight:800,color:active?t.acc:t.text,whiteSpace:"nowrap",overflow:"hidden",textOverflow:"ellipsis"}}>{label}</span>
              <span style={{display:"block",fontSize:9,color:t.mut,marginTop:2,whiteSpace:"nowrap",overflow:"hidden",textOverflow:"ellipsis"}}>{hint}</span>
            </span>
          </button>;})}
        </div>
        {(()=>{const active=settingsTab==="changelog";return <button onClick={async()=>{setSettingsTab("changelog");setShowHealthMonitor(false);if(!changelogContent){try{const r=await fetch(`${API}/api/changelog`);const d=await r.json();setChangelogContent(d.content||"");}catch{setChangelogContent("# Changelog\n\nFailed to load.");}}}} style={{width:"100%",display:"flex",alignItems:"center",gap:10,textAlign:"left",padding:"10px 11px",borderRadius:10,border:`1px solid ${active?t.acc:t.brd}33`,background:active?`${t.acc}18`:`${t.bgDeep}88`,color:active?t.acc:t.dim,cursor:"pointer",fontFamily:font,boxShadow:"none",marginTop:8}}>
          <span style={{fontSize:16,width:20,textAlign:"center",flexShrink:0}}>📋</span>
          <span style={{minWidth:0,flex:1}}>
            <span style={{display:"block",fontSize:12,fontWeight:800,color:active?t.acc:t.text,whiteSpace:"nowrap",overflow:"hidden",textOverflow:"ellipsis"}}>Changelog</span>
            <span style={{display:"block",fontSize:9,color:t.mut,marginTop:2,whiteSpace:"nowrap",overflow:"hidden",textOverflow:"ellipsis"}}>Release notes and updates</span>
          </span>
        </button>;})()}
      </div>
      <div style={{display:"flex",overflow:"hidden",minWidth:0}}>
    <div style={{flex:showHealthMonitor?"0 0 52%":"1 1 auto",overflowY:"auto",padding:"22px 30px",transition:"flex .3s ease",minWidth:0}}>
    <div style={{maxWidth:980}}>
      <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:18}}>
        <h3 style={{fontSize:16,fontWeight:700,color:t.acc,letterSpacing:1,textTransform:"uppercase",margin:0}}>{activeSettingsTitle}</h3>
        <div style={{display:"flex",alignItems:"center",gap:8}}>
          {settingsPulse&&<span style={{fontSize:10,color:settingsPulse.type==="error"?t.err:t.ok,border:`1px solid ${settingsPulse.type==="error"?t.err:t.ok}33`,background:`${settingsPulse.type==="error"?t.err:t.ok}12`,borderRadius:999,padding:"3px 8px",fontWeight:700,animation:"fadeIn .15s"}}>{settingsPulse.text}</span>}
          {settingsTab==="changelog"&&<button onClick={async()=>{setChangelogContent(null);try{const r=await fetch(`${API}/api/changelog`);const d=await r.json();setChangelogContent(d.content||"");}catch{setChangelogContent("# Changelog\n\nFailed to load.");}}} style={{...btnS(t.f1),fontSize:11,padding:"5px 10px"}}>Refresh</button>}
          <button onClick={closeSettings} style={{...btnS(t.mut),fontSize:12,padding:"6px 10px"}}>✕</button>
        </div>
      </div>

      {/* TILE: Users */}
      <div style={{...settingsCardS,display:settingsTab==="users"?"block":"none"}}>
        <div style={{display:"flex",justifyContent:"space-between",gap:12,alignItems:"flex-start",marginBottom:14}}>
          <div>
            <div style={{fontSize:14,fontWeight:800,color:t.acc,letterSpacing:.5,display:"flex",alignItems:"center",gap:8}}>👤 Users</div>
            <div style={{...settingsKickerS,marginTop:4}}>Local profiles for keeping HyprChat data separate on this install.</div>
          </div>
          <button onClick={refreshUsers} style={{...btnS(t.mut),fontSize:11,padding:"5px 10px"}}><IC.Refresh/> Refresh</button>
        </div>
        <div style={{display:"flex",flexDirection:"column",gap:14}}>
          <section style={{background:t.bgDeep,border:`1px solid ${t.brd}24`,borderRadius:9,padding:"12px 14px",display:"flex",alignItems:"center",justifyContent:"space-between",gap:14}}>
            <div style={{minWidth:0}}>
              <div style={{fontSize:10,color:t.mut,textTransform:"uppercase",letterSpacing:.6,fontWeight:800,marginBottom:4}}>Current Session</div>
              <div style={{display:"flex",alignItems:"center",gap:8,minWidth:0}}>
                <span style={{fontSize:14,color:t.text,fontWeight:900,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{currentUser?.name||"None"}</span>
                {currentUser?.password_enabled&&<span style={{fontSize:9,color:t.warm,border:`1px solid ${t.warm}33`,background:`${t.warm}12`,borderRadius:999,padding:"1px 7px",fontWeight:800}}>Password</span>}
              </div>
            </div>
            <button onClick={logoutCurrentUser} style={{...btnS(t.warm),fontSize:12,padding:"8px 14px",whiteSpace:"nowrap"}}>Log Out</button>
          </section>

          {loginError&&<div style={{fontSize:11,color:t.err,background:`${t.err}12`,border:`1px solid ${t.err}24`,borderRadius:7,padding:"7px 9px"}}>{loginError}</div>}

          <section style={{borderTop:`1px solid ${t.brd}22`,paddingTop:14}}>
            <div style={{fontSize:10,color:t.mut,textTransform:"uppercase",letterSpacing:.6,fontWeight:800,marginBottom:8}}>Create Profile</div>
            <div style={{display:"grid",gridTemplateColumns:"minmax(180px,1fr) minmax(180px,1fr) 96px",gap:8,alignItems:"center"}}>
              <input value={newUserName} onChange={e=>setNewUserName(e.target.value)} onKeyDown={e=>{if(e.key==="Enter"&&newUserName.trim())createManagedUser();}} placeholder="New user name" style={{...inputS,fontSize:12}}/>
              <input type="password" value={newUserPassword} onChange={e=>setNewUserPassword(e.target.value)} onKeyDown={e=>{if(e.key==="Enter"&&newUserName.trim())createManagedUser();}} placeholder="Optional password" style={{...inputS,fontSize:12}}/>
              <button onClick={createManagedUser} disabled={!newUserName.trim()} style={{...btnS(t.ok),fontSize:12,padding:"8px 13px",height:38,opacity:newUserName.trim()?1:.45,whiteSpace:"nowrap",justifyContent:"center"}}><IC.Plus/> Create</button>
            </div>
          </section>

          <section style={{borderTop:`1px solid ${t.brd}22`,paddingTop:14}}>
            <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",gap:12,marginBottom:8}}>
              <div style={{fontSize:10,color:t.mut,textTransform:"uppercase",letterSpacing:.6,fontWeight:800}}>Manage Profiles</div>
              <div style={{fontSize:10,color:t.dim}}>{users.length} profile{users.length===1?"":"s"}</div>
            </div>
            <div style={{display:"flex",flexDirection:"column",gap:8}}>
              {users.map(u=>{const isCurrent=u.id===currentUserId;const nameDraft=userNameDrafts[u.id]??u.name??"";const pwdDraft=userPasswordDrafts[u.id]||"";return <div key={u.id} style={{background:t.bgDeep,border:`1px solid ${isCurrent?t.acc:t.brd}30`,borderRadius:9,padding:"11px 12px"}}>
                <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",gap:10,marginBottom:10}}>
                  <div style={{display:"flex",alignItems:"center",gap:7,minWidth:0}}>
                    <span style={{fontSize:10,color:isCurrent?t.acc:t.mut,fontWeight:900,textTransform:"uppercase",letterSpacing:.6,flexShrink:0}}>{isCurrent?"Current":"Profile"}</span>
                    <span style={{fontSize:13,color:t.text,fontWeight:800,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{u.name}</span>
                    {u.password_enabled&&<span style={{fontSize:9,color:t.warm,border:`1px solid ${t.warm}33`,background:`${t.warm}12`,borderRadius:999,padding:"1px 6px",fontWeight:800,flexShrink:0}}>Password</span>}
                    {u.id==="default"&&<span style={{fontSize:9,color:t.mut,border:`1px solid ${t.brd}33`,borderRadius:999,padding:"1px 6px",fontWeight:800,flexShrink:0}}>Main</span>}
                  </div>
                  {u.id!=="default"&&<button onClick={()=>deleteManagedUser(u)} title="Delete profile" style={{...btnS(t.err),fontSize:10,padding:"6px 8px",height:32,flexShrink:0}}><IC.Trash/></button>}
                </div>
                <div style={{display:"grid",gridTemplateColumns:"minmax(180px,1fr) 74px minmax(180px,1fr) 74px minmax(72px,auto)",gap:8,alignItems:"center"}}>
                  <input value={nameDraft} onChange={e=>setUserNameDrafts(p=>({...p,[u.id]:e.target.value}))} placeholder="Display name" style={{...inputS,fontSize:12,padding:"8px 10px"}}/>
                  <button onClick={()=>updateManagedUser(u.id,{name:nameDraft})} disabled={!nameDraft.trim()||nameDraft===u.name} style={{...btnS(t.f1),fontSize:10,padding:"6px 9px",height:36,opacity:nameDraft.trim()&&nameDraft!==u.name?1:.45,justifyContent:"center"}}>Save</button>
                  <input type="password" value={pwdDraft} onChange={e=>setUserPasswordDrafts(p=>({...p,[u.id]:e.target.value}))} placeholder={u.password_enabled?"New password":"Set password"} style={{...inputS,fontSize:12,padding:"8px 10px"}}/>
                  <button onClick={()=>updateManagedUser(u.id,{password:pwdDraft})} disabled={!pwdDraft} style={{...btnS(t.warm),fontSize:10,padding:"6px 9px",height:36,opacity:pwdDraft?1:.45,whiteSpace:"nowrap",justifyContent:"center"}}>Set</button>
                  {u.password_enabled?<button onClick={()=>updateManagedUser(u.id,{clear_password:true})} style={{...btnS(t.mut),fontSize:10,padding:"6px 9px",height:36,whiteSpace:"nowrap",justifyContent:"center"}}>Clear</button>:<span/>}
                </div>
              </div>;})}
            </div>
          </section>
        </div>
      </div>

      {/* TILE: Connections */}
      <div style={{...settingsCardS,display:settingsTab==="connections"?"block":"none"}}>
        <div style={{display:"flex",justifyContent:"space-between",gap:12,alignItems:"flex-start",marginBottom:12}}>
          <div>
            <div style={{fontSize:14,fontWeight:800,color:t.acc,letterSpacing:.5,display:"flex",alignItems:"center",gap:8}}>🔌 Connections</div>
            <div style={{...settingsKickerS,marginTop:4}}>Runtime endpoints and live service status.</div>
          </div>
          <div style={{display:"flex",gap:6,flexWrap:"wrap",justifyContent:"flex-end"}}>
            <button onClick={async()=>{
              const next=!showHealthMonitor;
              setShowHealthMonitor(next);
              if(next&&!healthHistory){try{const r=await fetch(`${API}/api/health/history?days=90`);if(!r.ok)throw new Error(`HTTP ${r.status}`);const d=await r.json();setHealthHistory(d);}catch(e){console.error("Health history:",e);notify({type:"warning",text:"Health history failed",detail:e.message||String(e),duration:5000});}}
            }} style={{padding:"5px 11px",borderRadius:7,border:`1px solid ${showHealthMonitor?t.acc:t.brd}44`,background:showHealthMonitor?`${t.acc}22`:t.bgDeep,color:showHealthMonitor?t.acc:t.dim,fontSize:11,cursor:"pointer",fontFamily:font,fontWeight:700,display:"flex",alignItems:"center",gap:5}}>
              <span style={{fontSize:13}}>{showHealthMonitor?"◂":"▸"}</span> Monitor
            </button>
          </div>
        </div>
        <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",gap:10,marginBottom:14}}>
          <div style={{display:"flex",flexWrap:"wrap",gap:6}}>
            {["ollama","codebox","n8n","searxng","comfyui","stt","tts"].filter(n=>health[n]!==undefined||["ollama","codebox","n8n","searxng"].includes(n)).map(n=>{const s=health[n]||{};const rl=s?.rate_limited;const c=s?.status==="ok"?t.ok:s?.status==="degraded"?"#f0a030":s?.status?t.err:t.mut;const label={ollama:"Ollama",codebox:"Codebox",n8n:"N8N",searxng:"SearXNG",comfyui:"ComfyUI",stt:"Voice STT",tts:"Voice TTS"}[n];return <div key={n} title={`${label}: ${s?.status||"unknown"}${s?.response_ms!=null?` (${s.response_ms}ms)`:""}`} style={{display:"flex",alignItems:"center",gap:6,padding:"5px 11px",background:t.bgDeep,borderRadius:7,border:`1px solid ${rl?`#f0a030`:t.brd}24`,fontSize:12}}>
              <div style={{width:8,height:8,borderRadius:"50%",background:c,boxShadow:`0 0 6px ${c}66`}}/>
              <span style={{fontWeight:700,color:t.dim}}>{label}</span>
              {s?.response_ms!=null&&<span style={{fontSize:10,color:t.mut}}>{s.response_ms}ms</span>}
              {rl&&<span style={{padding:"1px 6px",borderRadius:4,background:"#f0a03020",border:"1px solid #f0a03044",fontSize:9,fontWeight:800,color:"#f0a030"}}>Limited</span>}
            </div>;})}
          </div>
        </div>
        <div style={{borderTop:`1px solid ${t.brd}22`,paddingTop:14,marginBottom:14}}>
          <div style={{display:"flex",justifyContent:"space-between",gap:12,alignItems:"baseline",marginBottom:10}}>
            <div>
              <div style={{fontSize:12,fontWeight:900,color:t.dim,textTransform:"uppercase",letterSpacing:.7}}>Cloud Models</div>
              <div style={{fontSize:10,color:t.mut,marginTop:3}}>API keys are stored for the current HyprChat user.</div>
            </div>
            <button onClick={async()=>{await refreshModelProviders();await refreshModels();}} style={{...btnS(t.f1),fontSize:10,padding:"5px 9px"}}><IC.Refresh/> Refresh</button>
          </div>
          <div style={{display:"grid",gridTemplateColumns:"1fr",gap:10}}>
            {[["openai","OpenAI","sk-...","ChatGPT / GPT models"],["anthropic","Anthropic","sk-ant-...","Claude models"]].map(([provider,label,placeholder,hint])=>{
              const st=modelProviders[provider]||{provider,label,enabled:false,has_key:false,key_source:"none",key_hint:""};
              const busy=providerBusy[provider];
              const enabled=!!st.enabled;
              const key=providerKeys[provider]||"";
              const statusColor=enabled&&st.has_key?t.ok:st.has_key?t.warm:t.mut;
              return <div key={provider} style={{display:"grid",gridTemplateColumns:"120px minmax(0,1fr) auto",gap:10,alignItems:"center",background:t.bgDeep,border:`1px solid ${t.brd}24`,borderRadius:8,padding:"10px 11px"}}>
                <div>
                  <div style={{display:"flex",alignItems:"center",gap:7,fontSize:12,fontWeight:900,color:t.text}}><span>{provider==="openai"?"◎":"✦"}</span>{label}</div>
                  <div style={{fontSize:9,color:t.mut,marginTop:3}}>{hint}</div>
                </div>
                <div style={{display:"grid",gridTemplateColumns:"minmax(180px,1fr) auto",gap:8,alignItems:"center"}}>
                  <input type="password" value={key} onChange={e=>setProviderKeys(p=>({...p,[provider]:e.target.value}))} placeholder={st.key_hint?`Saved ${st.key_hint}`:placeholder} style={{...inputS,fontFamily:"monospace",fontSize:12,padding:"8px 10px"}}/>
                  <label style={{display:"flex",alignItems:"center",gap:6,fontSize:11,color:enabled?t.ok:t.mut,cursor:"pointer",whiteSpace:"nowrap"}}>
                    <input type="checkbox" checked={enabled} onChange={e=>saveModelProvider(provider,{enabled:e.target.checked})} disabled={!!busy}/>
                    Enabled
                  </label>
                  <div style={{gridColumn:"1 / -1",display:"flex",gap:8,alignItems:"center",fontSize:9,color:statusColor}}>
                    <span style={{width:7,height:7,borderRadius:"50%",background:statusColor,boxShadow:`0 0 6px ${statusColor}66`}}/>
                    <span>{st.has_key?(enabled?`Ready (${st.key_source})`:`Key disabled`):"No key saved"}</span>
                    {st.last_test_status==="ok"&&<span style={{color:t.ok}}>Test passed</span>}
                    {st.last_test_status==="error"&&<span title={st.last_test_error||""} style={{color:t.err}}>Test failed</span>}
                  </div>
                </div>
                <div style={{display:"flex",gap:6,justifyContent:"flex-end",flexWrap:"wrap"}}>
                  <button onClick={()=>testModelProvider(provider)} disabled={!!busy||(!key&&!st.has_key)} style={{...btnS(t.f1),fontSize:10,padding:"6px 9px",opacity:(busy||(!key&&!st.has_key))?0.5:1}}>{busy==="testing"?"Testing...":"Test"}</button>
                  <button onClick={()=>saveModelProvider(provider,{enabled})} disabled={!!busy||!key} style={{...btnS(t.ok),fontSize:10,padding:"6px 9px",opacity:(busy||!key)?0.5:1}}>{busy==="saving"?"Saving...":"Save"}</button>
                  <button onClick={()=>deleteModelProvider(provider)} disabled={!!busy||!st.has_key} style={{...btnS(t.err),fontSize:10,padding:"6px 8px",opacity:(busy||!st.has_key)?0.45:1}}><IC.Trash/></button>
                </div>
              </div>;
            })}
          </div>
        </div>
        <div style={{borderTop:`1px solid ${t.brd}22`,paddingTop:14}}>
          <div style={{display:"grid",gridTemplateColumns:"1fr",gap:12}}>
            {[
              ["Ollama",ollamaUrl,setOllamaUrl,"http://192.168.1.110:11434","Model inference and model manager"],
              ["Codebox",codeboxUrl,setCodeboxUrl,"http://192.168.1.201:8585","Tool execution and project upload bridge"],
              ["N8N",n8nUrl,setN8nUrl,"http://192.168.1.114:5678","External workflow automation"],
              ["SearXNG",searxngUrl,setSearxngUrl,"http://192.168.1.141:8888","Search and research backend"],
              ["ComfyUI",comfyuiUrl,setComfyuiUrl,"http://192.168.1.115:8188","Stable Diffusion image generation (optional)"],
              ["Voice STT",sttUrl,setSttUrl,"http://192.168.1.115:8001","Speech-to-text — Speaches/Whisper (optional)"],
              ["Voice TTS",ttsUrl,setTtsUrl,"http://192.168.1.115:8880","Text-to-speech — Kokoro (optional)"],
            ].map(([label,value,setter,placeholder,hint])=><label key={label} style={{display:"grid",gridTemplateColumns:"120px minmax(0,1fr)",gap:12,alignItems:"center"}}>
              <div style={{display:"flex",alignItems:"baseline",justifyContent:"space-between",gap:8,marginBottom:5}}>
                <span style={{fontSize:12,color:t.dim,fontWeight:700}}>{label}</span>
                <span style={{fontSize:9,color:t.mut,display:"none"}}>{hint}</span>
              </div>
              <div>
                <input value={value} onChange={e=>setter(e.target.value)} placeholder={placeholder} style={{...inputS,fontFamily:"monospace",fontSize:12,padding:"8px 10px"}}/>
                <div style={{fontSize:9,color:t.mut,marginTop:4}}>{hint}</div>
              </div>
            </label>)}
          </div>
          <div style={{marginTop:14,padding:"12px 13px",background:`${t.surface}44`,border:`1px solid ${t.brd}33`,borderRadius:8}}>
            <div style={{display:"flex",justifyContent:"space-between",gap:10,alignItems:"center",marginBottom:10}}>
              <div>
                <div style={{fontSize:11,fontWeight:900,color:t.warm,textTransform:"uppercase",letterSpacing:.7}}>Ollama Hardware Scan SSH</div>
                <div style={{fontSize:9,color:t.mut,marginTop:3}}>Used only by HyprFit Rescan Hardware for remote Ollama hosts.</div>
              </div>
              <span style={mmChipS(ollamaScanSshAuthMode==="password"&&ollamaScanSshHasPassword?t.ok:ollamaScanSshAuthMode==="key"&&ollamaScanSshKeyPath?t.ok:t.mut,`${t.bgDeep}AA`)}>
                {ollamaScanSshAuthMode==="password"?(ollamaScanSshHasPassword?"password saved":"password needed"):(ollamaScanSshKeyPath?"key configured":"key needed")}
              </span>
            </div>
            <div style={{display:"grid",gridTemplateColumns:"minmax(130px,1fr) 90px minmax(100px,.8fr) 120px",gap:9,alignItems:"end"}}>
              <label>
                <span style={{display:"block",fontSize:9,color:t.mut,textTransform:"uppercase",letterSpacing:.5,fontWeight:800,marginBottom:4}}>Host</span>
                <input value={ollamaScanSshHost} onChange={e=>setOllamaScanSshHost(e.target.value)} placeholder="derived from Ollama URL" style={{...inputS,fontFamily:"monospace",fontSize:12,padding:"8px 10px"}}/>
              </label>
              <label>
                <span style={{display:"block",fontSize:9,color:t.mut,textTransform:"uppercase",letterSpacing:.5,fontWeight:800,marginBottom:4}}>Port</span>
                <input type="number" min={1} max={65535} value={ollamaScanSshPort} onChange={e=>setOllamaScanSshPort(e.target.value)} style={{...inputS,fontFamily:"monospace",fontSize:12,padding:"8px 10px"}}/>
              </label>
              <label>
                <span style={{display:"block",fontSize:9,color:t.mut,textTransform:"uppercase",letterSpacing:.5,fontWeight:800,marginBottom:4}}>User</span>
                <input value={ollamaScanSshUser} onChange={e=>setOllamaScanSshUser(e.target.value)} placeholder="root" style={{...inputS,fontFamily:"monospace",fontSize:12,padding:"8px 10px"}}/>
              </label>
              <label>
                <span style={{display:"block",fontSize:9,color:t.mut,textTransform:"uppercase",letterSpacing:.5,fontWeight:800,marginBottom:4}}>Auth</span>
                <select value={ollamaScanSshAuthMode} onChange={e=>setOllamaScanSshAuthMode(e.target.value)} style={{...inputS,fontSize:12,padding:"8px 10px"}}>
                  <option value="key">SSH key</option>
                  <option value="password">Password</option>
                </select>
              </label>
            </div>
            {ollamaScanSshAuthMode==="key"?<label style={{display:"block",marginTop:9}}>
              <span style={{display:"block",fontSize:9,color:t.mut,textTransform:"uppercase",letterSpacing:.5,fontWeight:800,marginBottom:4}}>Key path</span>
              <input value={ollamaScanSshKeyPath} onChange={e=>setOllamaScanSshKeyPath(e.target.value)} placeholder="/root/.ssh/id_ed25519" style={{...inputS,fontFamily:"monospace",fontSize:12,padding:"8px 10px"}}/>
            </label>:<div style={{display:"grid",gridTemplateColumns:"minmax(0,1fr) auto",gap:8,alignItems:"end",marginTop:9}}>
              <label>
                <span style={{display:"block",fontSize:9,color:t.mut,textTransform:"uppercase",letterSpacing:.5,fontWeight:800,marginBottom:4}}>Password</span>
                <input type="password" value={ollamaScanSshPassword} onChange={e=>{setOllamaScanSshPassword(e.target.value);setOllamaScanSshClearPassword(false);}} placeholder={ollamaScanSshHasPassword?"Saved password":"SSH password"} style={{...inputS,fontFamily:"monospace",fontSize:12,padding:"8px 10px"}}/>
              </label>
              <button type="button" onClick={()=>{setOllamaScanSshPassword("");setOllamaScanSshHasPassword(false);setOllamaScanSshClearPassword(true);}} disabled={!ollamaScanSshHasPassword&&!ollamaScanSshPassword} style={{...btnS(t.err),fontSize:10,padding:"7px 10px",opacity:(!ollamaScanSshHasPassword&&!ollamaScanSshPassword)?0.45:1}}>Clear</button>
            </div>}
          </div>
          {(ttsUrl||sttUrl)&&<div style={{marginTop:12,padding:"10px 12px",background:`${t.surface}44`,border:`1px solid ${t.brd}33`,borderRadius:8,display:"flex",gap:16,alignItems:"center",flexWrap:"wrap"}}>
            <span style={{fontSize:11,fontWeight:700,color:t.dim}}>🎙 Voice</span>
            {ttsUrl&&<label style={{display:"flex",alignItems:"center",gap:6,fontSize:11,color:t.dim}}>
              Voice:
              <select value={ttsVoice} onChange={e=>setTtsVoice(e.target.value)} style={{...inputS,width:"auto",fontSize:11,padding:"4px 8px"}}>
                {ttsVoice&&!ttsVoices.includes(ttsVoice)&&<option value={ttsVoice}>{ttsVoice}</option>}
                {ttsVoices.map(v=><option key={v} value={v}>{v}</option>)}
              </select>
            </label>}
            {ttsUrl&&<label style={{display:"flex",alignItems:"center",gap:6,fontSize:11,color:t.dim,cursor:"pointer"}}>
              <input type="checkbox" checked={ttsAutoplay} onChange={e=>{setTtsAutoplay(e.target.checked);localStorage.setItem("hc-tts-autoplay",e.target.checked?"1":"0");}}/>
              Auto-play replies
            </label>}
            <span style={{fontSize:9,color:t.mut}}>Mic capture needs a secure context — over plain HTTP, allow this origin in chrome://flags/#unsafely-treat-insecure-origin-as-secure</span>
          </div>}
          <div style={{display:"flex",justifyContent:"space-between",gap:10,alignItems:"center",marginTop:12}}>
            <div style={settingsKickerS}>Bare <code>IP:port</code> is accepted; HyprChat stores it as an HTTP URL.</div>
            <button disabled={connectionsSaving} onClick={async()=>{
              setConnectionsSaving(true);setConnectionsSaveState("saving");
              try{
                const payload={ollama_url:ollamaUrl,codebox_url:codeboxUrl,searxng_url:searxngUrl,n8n_url:n8nUrl,comfyui_url:comfyuiUrl,stt_url:sttUrl,tts_url:ttsUrl,tts_voice:ttsVoice,ollama_scan_ssh_host:ollamaScanSshHost,ollama_scan_ssh_port:Number(ollamaScanSshPort)||22,ollama_scan_ssh_user:ollamaScanSshUser,ollama_scan_ssh_auth_mode:ollamaScanSshAuthMode,ollama_scan_ssh_key_path:ollamaScanSshKeyPath};
                if(ollamaScanSshPassword||ollamaScanSshClearPassword)payload.ollama_scan_ssh_password=ollamaScanSshPassword;
                const r=await fetch(`${API}/api/settings`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
                if(!r.ok)throw new Error(`HTTP ${r.status}`);
                const d=await r.json().catch(()=>({}));
                if(d.current_ollama_url)setOllamaUrl(d.current_ollama_url);
                if(d.current_codebox_url)setCodeboxUrl(d.current_codebox_url);
                if(d.current_searxng_url)setSearxngUrl(d.current_searxng_url);
                if(d.current_n8n_url)setN8nUrl(d.current_n8n_url);
                if(d.current_comfyui_url!==undefined)setComfyuiUrl(d.current_comfyui_url||"");
                if(d.current_stt_url!==undefined)setSttUrl(d.current_stt_url||"");
                if(d.current_tts_url!==undefined)setTtsUrl(d.current_tts_url||"");
                if(d.current_tts_url)fetch(`${API}/api/audio/voices`).then(rv=>rv.json()).then(v=>setTtsVoices(v.voices||[])).catch(()=>{});
                if(d.ollama_scan_ssh_host!==undefined)setOllamaScanSshHost(d.ollama_scan_ssh_host||"");
                if(d.ollama_scan_ssh_port!=null)setOllamaScanSshPort(Number(d.ollama_scan_ssh_port)||22);
                if(d.ollama_scan_ssh_user!==undefined)setOllamaScanSshUser(d.ollama_scan_ssh_user||"root");
                if(d.ollama_scan_ssh_auth_mode)setOllamaScanSshAuthMode(d.ollama_scan_ssh_auth_mode==="password"?"password":"key");
                if(d.ollama_scan_ssh_key_path!==undefined)setOllamaScanSshKeyPath(d.ollama_scan_ssh_key_path||"");
                setOllamaScanSshHasPassword(!!d.ollama_scan_ssh_has_password);
                setOllamaScanSshPassword("");
                setOllamaScanSshClearPassword(false);
                await refreshModels();
                try{const h=await fetch(`${API}/api/health`);const hd=await h.json();setHealth(hd.services||{});}catch{}
                setConnectionsSaveState("saved");notify({type:"success",text:"Connections saved"});
                setTimeout(()=>setConnectionsSaveState(null),2200);
              }catch(e){console.error("Connection settings:",e);setConnectionsSaveState("failed");notify({type:"error",text:"Connection save failed",detail:e.message});}
              setConnectionsSaving(false);
            }} style={{...btnS(t.ok),opacity:connectionsSaving ? .6 : 1,fontSize:12,padding:"8px 16px",whiteSpace:"nowrap"}}>
              {connectionsSaving?"Saving...":"Save Connections"}
            </button>
          </div>
          {connectionsSaveState&&<div style={{fontSize:10,color:connectionsSaveState==="failed"?t.err:connectionsSaveState==="saved"?t.ok:t.mut,marginTop:6,textAlign:"right"}}>{connectionsSaveState==="saving"?"Saving...":connectionsSaveState==="saved"?"Saved":"Failed"}</div>}
        </div>
      </div>

      {/* TILE: Model & Generation */}
      <div style={{...settingsCardS,display:settingsTab==="generation"?"block":"none"}}>
        <div style={{fontSize:14,fontWeight:800,color:t.acc,letterSpacing:.5,marginBottom:4,display:"flex",alignItems:"center",gap:8}}>Model & Generation</div>
        <div style={{...settingsKickerS,marginBottom:14}}>Global model behavior. Individual chats can still override effort and tools.</div>

        <div style={{marginBottom:14}}>
          <div style={{fontSize:12,color:t.dim,marginBottom:6,fontWeight:600}}>Default Context Window</div>
          <div style={{display:"flex",flexWrap:"wrap",gap:5}}>
            {[0,4096,8192,16384,32768,65536,131072,262144].map(v=><button key={v} onClick={()=>setNumCtx(v)} style={{padding:"6px 14px",borderRadius:7,border:`1px solid ${numCtx===v?t.acc:t.brd}44`,background:numCtx===v?`${t.acc}22`:t.bgDeep,color:numCtx===v?t.acc:t.dim,fontFamily:font,fontSize:12,cursor:"pointer",fontWeight:numCtx===v?700:400}}>
              {v===0?"Auto":v>=1024?`${(v/1024).toFixed(0)}K`:v}
            </button>)}
          </div>
          {numCtx>0&&<div style={{fontSize:11,color:t.acc,marginTop:4,fontWeight:600}}>{numCtx>=1024?`${(numCtx/1024).toFixed(0)}K`:numCtx} tokens</div>}
          <div style={{fontSize:10,color:t.mut,marginTop:4}}>Sets num_ctx for all chats. Per-model overrides in Model Manager.</div>
        </div>

        <div style={{marginBottom:14}}>
          <div style={{fontSize:12,color:t.dim,marginBottom:6,fontWeight:600}}>Thinking Mode</div>
          <div style={{display:"flex",gap:4}}>
            {["auto","on","off"].map(v=><button key={v} onClick={()=>setThinkMode(v)} style={{padding:"5px 12px",borderRadius:7,border:`1px solid ${thinkMode===v?t.acc:t.brd}44`,background:thinkMode===v?`${t.acc}22`:t.bgDeep,color:thinkMode===v?t.acc:t.dim,fontFamily:font,fontSize:12,cursor:"pointer",fontWeight:thinkMode===v?700:400}}>{v==="auto"?"Auto":v==="on"?"On":"Off"}</button>)}
          </div>
          <div style={{fontSize:10,color:t.mut,marginTop:4}}>Controls whether models use thinking tokens. Auto lets the model decide.</div>
        </div>

        <div style={{marginBottom:14}}>
          <div style={{fontSize:12,color:t.dim,marginBottom:6,fontWeight:600}}>Default Effort Level</div>
          <div style={{display:"flex",flexWrap:"wrap",gap:5}}>
            {EFFORT_LEVELS.map((lv,v)=><button key={v} title={lv.desc} onClick={()=>setGlobalEffort(v)} style={{padding:"6px 12px",borderRadius:7,border:`1px solid ${globalEffort===v?t.pink:t.brd}44`,background:globalEffort===v?`${t.pink}22`:t.bgDeep,color:globalEffort===v?t.pink:t.dim,fontFamily:font,fontSize:12,cursor:"pointer",fontWeight:globalEffort===v?700:400,display:"inline-flex",alignItems:"center",gap:5}}>
              <span style={{fontSize:14}}>{lv.emoji}</span>{lv.name}
            </button>)}
          </div>
          <div style={{fontSize:10,color:t.mut,marginTop:4}}>After the initial answer, the model re-examines and refines it. {EFFORT_LEVELS[globalEffort].name} = {globalEffort===0?"no review":`${globalEffort} review pass${globalEffort>1?"es":""}`}. Each chat can override this.</div>
        </div>

        <div style={{borderTop:`1px solid ${t.brd}22`,paddingTop:14,marginBottom:6}}>
          <div style={{fontSize:12,color:t.dim,marginBottom:8,fontWeight:600}}>Workspace Analysis Model</div>
          <ModelPicker value={wsModel} onChange={setWsModel} models={models} modelDetails={modelDetails} t={t} font={font}/>
          <div style={{fontSize:10,color:t.mut,marginTop:6}}>Small, fast model for workspace topic auto-detection.</div>
        </div>

        <div style={{borderTop:`1px solid ${t.brd}22`,paddingTop:14,marginBottom:6}}>
          <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:5}}>
            <span style={{display:"flex",color:t.acc}}><IC.Image/></span>
            <div style={{fontSize:12,color:t.dim,fontWeight:800,letterSpacing:.35}}>Chat Image Generation</div>
            <span style={{fontSize:9,color:t.acc,background:`${t.acc}12`,border:`1px solid ${t.acc}28`,borderRadius:6,padding:"2px 6px",fontWeight:800}}>ComfyUI</span>
          </div>
          <div style={{fontSize:10,color:t.mut,marginBottom:10,lineHeight:1.5}}>Defaults for the in-chat <code>generate_image</code> tool. Image Studio ★ presets still apply automatically; this section controls the chat-facing model, workflow, prompt defaults, and optional prompt-writer model.</div>
          {!comfyuiUrl
            ?<div style={{fontSize:11,color:t.mut}}>ComfyUI is not configured — set its URL in Settings → Connections first.</div>
            :(()=>{
              const sel={background:t.bgDeep,border:`1px solid ${t.brd}44`,color:t.text,padding:"8px 10px",borderRadius:7,fontFamily:font,fontSize:12,outline:"none",boxSizing:"border-box"};
              const inp={...sel,width:"100%"};
              const imagePanelS={background:`${t.surface}44`,border:`1px solid ${t.brd}26`,borderRadius:10,padding:12};
              const imageSectionS={background:`${t.bgDeep}70`,border:`1px solid ${t.brd}24`,borderRadius:8,padding:12,display:"flex",flexDirection:"column",gap:10};
              const imageHeadS={display:"flex",alignItems:"baseline",justifyContent:"space-between",gap:10,borderBottom:`1px solid ${t.brd}18`,paddingBottom:8};
              const imageTitleS={fontSize:10,fontWeight:900,color:t.acc,textTransform:"uppercase",letterSpacing:.75};
              const imageHintS={fontSize:9.5,color:t.mut,lineHeight:1.45};
              const fieldLabelS={fontSize:10,color:t.mut,fontWeight:800,textTransform:"uppercase",letterSpacing:.55,display:"flex",flexDirection:"column",gap:5,minWidth:0};
              const fieldGridS={display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(190px,1fr))",gap:10,alignItems:"end"};
              const cks=imgChatLists?.checkpoints||[];
              const vaeList=imgChatLists?.vaes||[];
              const hasPreset=!!(imgChatLists?.settings?.[imgChatCkpt]?.user_override);
              const selectedModelLabel=imgChatCkpt?imgChatCkpt.replace(/\.(safetensors|ckpt)$/i,""):"global fallback";
              return <div style={imagePanelS}>
                <div style={{display:"grid",gridTemplateColumns:"minmax(0,1fr)",gap:10}}>
                  <section style={imageSectionS}>
                    <div style={imageHeadS}>
                      <div style={imageTitleS}>Generation Target</div>
                      {hasPreset&&<div title="Has saved Image Studio defaults" style={{fontSize:9,color:t.acc,fontWeight:800}}>★ preset</div>}
                    </div>
                    <div style={fieldGridS}>
                      <label style={fieldLabelS}>Image model
                        <select value={imgChatCkpt} onChange={e=>setImgChatCkpt(e.target.value)} style={{...sel,width:"100%"}}>
                      <option value="">(none — built-in SDXL template)</option>
                      {cks.map(c=><option key={c} value={c}>{c.replace(/\.(safetensors|ckpt)$/i,"")}</option>)}
                      {imgChatCkpt&&imgChatLists&&!cks.includes(imgChatCkpt)&&<option value={imgChatCkpt}>{imgChatCkpt} (missing!)</option>}
                    </select>
                      </label>
                      <label style={fieldLabelS}>Resolution
                        <select value={imgChatRes} onChange={e=>setImgChatRes(e.target.value)} style={{...sel,width:"100%"}}>
                      <option value="1024x1024">Square — 1024 × 1024</option>
                      <option value="1216x832">Landscape — 1216 × 832</option>
                      <option value="832x1216">Portrait — 832 × 1216</option>
                    </select>
                      </label>
                      <label style={fieldLabelS}>VAE
                        <select value={imgChatVae} onChange={e=>setImgChatVae(e.target.value)} style={{...sel,width:"100%"}}>
                      <option value="">Baked (checkpoint)</option>
                      {vaeList.map(v=><option key={v} value={v}>{v.replace(/\.(safetensors|pt|ckpt)$/i,"")}</option>)}
                    </select>
                      </label>
                      <label style={fieldLabelS}>Workflow
                        <select value={imgChatWorkflow} onChange={e=>setImgChatWorkflow(e.target.value)} style={{...sel,width:"100%"}}>
                      <option value="">Default (built-in SDXL)</option>
                      {imgChatWorkflows.map(w=><option key={w.name} value={w.name}>{w.name}{w.has_lora?" ⚡":""}</option>)}
                      {imgChatWorkflow&&!imgChatWorkflows.some(w=>w.name===imgChatWorkflow)&&<option value={imgChatWorkflow}>{imgChatWorkflow} (missing!)</option>}
                    </select>
                      </label>
                    </div>
                    <div style={imageHintS}>A saved workflow renders every chat image instead of the built-in template. Persona-specific image workflows still take priority.</div>
                  </section>

                  <section style={imageSectionS}>
                    <div style={imageHeadS}>
                      <div style={imageTitleS}>Prompt Defaults</div>
                      <div style={{fontSize:9,color:t.mut,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",maxWidth:260}}>{selectedModelLabel}</div>
                    </div>
                    {imgChatCkpt?<>
                  <div>
                    <div style={{fontSize:10,color:t.mut,marginBottom:5}}>Default prompt saved with this model</div>
                    <input value={mdlPrefix} onChange={e=>setMdlPrefix(e.target.value)} onBlur={saveModelPrefix} placeholder="e.g. score_9, score_8_up, realistic, 35mm photo — or anime style tags for anime models" style={inp}/>
                  </div>
                  <div>
                    <div style={{fontSize:10,color:t.mut,marginBottom:5}}>Default negative prompt for this model</div>
                    <input value={mdlNeg} onChange={e=>setMdlNeg(e.target.value)} onBlur={saveModelPrefix} placeholder="e.g. score_4, score_3, score_2, score_1, lowres, watermark" style={inp}/>
                  </div>
                </>:<>
                  <div>
                    <div style={{fontSize:10,color:t.mut,marginBottom:5}}>Default prompt (global fallback)</div>
                    <input value={imgChatPrefix} onChange={e=>setImgChatPrefix(e.target.value)} onBlur={()=>persistServerSetting("hc-img-chat-prefix","image_chat_prompt_prefix",imgChatPrefix)} placeholder="style/quality tags prepended to every chat image" style={inp}/>
                  </div>
                  <div>
                    <div style={{fontSize:10,color:t.mut,marginBottom:5}}>Default negative prompt (global fallback)</div>
                    <input value={imgChatNeg} onChange={e=>setImgChatNeg(e.target.value)} onBlur={()=>persistServerSetting("hc-img-chat-neg","image_chat_negative",imgChatNeg)} placeholder="things to avoid in every chat image" style={inp}/>
                  </div>
                </>}
                    <div style={imageHintS}>Text fields save when you click away. Pony-family models get score-tag prompts automatically unless overridden here.</div>
                  </section>

                  <section style={imageSectionS}>
                    <div style={imageHeadS}>
                      <div style={imageTitleS}>Prompt Model</div>
                      <div style={{fontSize:9,color:t.mut}}>persona photos + enhance</div>
                    </div>
                  {imgChatComposeModel?<div style={{display:"grid",gridTemplateColumns:"minmax(0,1fr) auto",gap:8,alignItems:"center"}}>
                    <div style={{flex:1}}><ModelPicker value={imgChatComposeModel} onChange={setImgChatComposeModel} models={models} modelDetails={modelDetails} t={t} font={font}/></div>
                    <button onClick={()=>setImgChatComposeModel("")} style={{padding:"7px 10px",background:`${t.err}14`,border:`1px solid ${t.err}35`,borderRadius:7,color:t.err,fontSize:10,cursor:"pointer",fontWeight:800,whiteSpace:"nowrap",fontFamily:font}}>Reset</button>
                  </div>:<div onClick={()=>setImgChatComposeModel(models[0]||"")} style={{display:"flex",alignItems:"center",gap:6,padding:"5px 10px",background:t.bgDeep,border:`1px dashed ${t.brd}55`,borderRadius:8,cursor:"pointer"}}>
                    <span style={{fontSize:14}}>📷</span>
                    <div style={{flex:1}}><div style={{fontSize:11,fontWeight:600,color:t.mut}}>Same as the conversation's chat model</div><div style={{fontSize:9,color:t.dim}}>Click to pick a dedicated model</div></div>
                    <span style={{fontSize:9,color:t.mut}}>▾</span>
                  </div>}
                  <div style={imageHintS}>Empty uses the chat's own model, which is usually best for matching conversation tone and content rating.</div>
                  </section>
                </div>
              </div>;
            })()}
        </div>
      </div>

      {/* TILE: Daedalus — all coder-agent-related settings live here.
          Includes the umbrella Planning/Coder model pickers, the new collapsible
          per-agent model overrides card, and OpenHands runtime knobs. */}
      <div style={{...settingsCardS,display:settingsTab==="daedalus"?"block":"none"}}>
        <div style={{fontSize:14,fontWeight:800,color:t.acc,letterSpacing:.5,marginBottom:4,display:"flex",alignItems:"center",gap:8}}>🏛️ Daedalus</div>
        <div style={{...settingsKickerS,marginBottom:14}}>Coder-agent routing, model inheritance, and uploaded-project repair behavior.</div>

        <div style={{marginBottom:14}}>
          <div style={{fontSize:12,color:t.dim,marginBottom:8,fontWeight:600}}>Code Planning Model</div>
          {planningModel?<div style={{display:"flex",gap:6,alignItems:"center"}}>
            <div style={{flex:1}}><ModelPicker value={planningModel} onChange={setPlanningModel} models={models} modelDetails={modelDetails} t={t} font={font}/></div>
            <button onClick={()=>setPlanningModel("")} style={{padding:"6px 10px",background:`${t.err}18`,border:`1px solid ${t.err}33`,borderRadius:7,color:t.err,fontSize:10,cursor:"pointer",fontWeight:600,whiteSpace:"nowrap"}}>Reset</button>
          </div>:<div onClick={()=>setPlanningModel(models[0]||"")} style={{display:"flex",alignItems:"center",gap:6,padding:"5px 10px",background:t.bgDeep,border:`1px dashed ${t.brd}55`,borderRadius:8,cursor:"pointer",transition:"all .15s"}}>
            <span style={{fontSize:14}}>🧠</span>
            <div style={{flex:1}}><div style={{fontSize:11,fontWeight:600,color:t.mut}}>Same as chat model</div><div style={{fontSize:9,color:t.dim}}>Click to select a dedicated planning model</div></div>
            <span style={{fontSize:9,color:t.mut}}>▾</span>
          </div>}
          <div style={{fontSize:10,color:t.mut,marginTop:6}}>Used by Architect + Reviewer + Acceptance agents. Empty = uses the active chat model.</div>
        </div>

        <div style={{borderTop:`1px solid ${t.brd}22`,paddingTop:14,marginBottom:14}}>
          <div style={{fontSize:12,color:t.dim,marginBottom:8,fontWeight:600}}>Code Generator Model</div>
          {coderModel?<div style={{display:"flex",gap:6,alignItems:"center"}}>
            <div style={{flex:1}}><ModelPicker value={coderModel} onChange={setCoderModel} models={models} modelDetails={modelDetails} t={t} font={font}/></div>
            <button onClick={()=>setCoderModel("")} style={{padding:"6px 10px",background:`${t.err}18`,border:`1px solid ${t.err}33`,borderRadius:7,color:t.err,fontSize:10,cursor:"pointer",fontWeight:600,whiteSpace:"nowrap"}}>Reset</button>
          </div>:<div onClick={()=>setCoderModel(models[0]||"")} style={{display:"flex",alignItems:"center",gap:6,padding:"5px 10px",background:t.bgDeep,border:`1px dashed ${t.brd}55`,borderRadius:8,cursor:"pointer",transition:"all .15s"}}>
            <span style={{fontSize:14}}>🧬</span>
            <div style={{flex:1}}><div style={{fontSize:11,fontWeight:600,color:t.mut}}>Same as chat model</div><div style={{fontSize:9,color:t.dim}}>Click to select a dedicated coder model</div></div>
            <span style={{fontSize:9,color:t.mut}}>▾</span>
          </div>}
          <div style={{fontSize:10,color:t.mut,marginTop:6}}>Used by Builder + Fixer agents. Empty = uses the active chat model.</div>
        </div>

        {/* Per-agent model overrides — collapsible advanced card.
            Each picker defaults to "Inherits from <umbrella>" until clicked.
            The reset button clears the override (returns to inheritance). */}
        <div style={{borderTop:`1px solid ${t.brd}22`,paddingTop:14,marginBottom:14}}>
          <div onClick={()=>setCoderBotModelsOpen(p=>!p)} style={{display:"flex",alignItems:"center",gap:8,cursor:"pointer",userSelect:"none"}}>
            <span style={{fontSize:14}}>🎛</span>
            <span style={{fontSize:12,fontWeight:700,color:t.text,letterSpacing:.3}}>Per-Agent Model Overrides</span>
            <span style={{flex:1}}/>
            <span style={{fontSize:10,color:t.mut}}>{coderBotModelsOpen ? "▴ collapse" : "▾ expand"}</span>
          </div>
          <div style={{fontSize:10,color:t.mut,marginTop:4}}>Optional. Pin a specific model per agent; empty rows inherit from the umbrella above.</div>

          {coderBotModelsOpen && <div style={{marginTop:14,paddingLeft:10,borderLeft:`2px solid ${t.acc}33`}}>
            {/* Architect — inherits from Planning Model */}
            <div style={{marginBottom:14}}>
              <div style={{fontSize:12,color:t.dim,marginBottom:6,fontWeight:600}}>📐 Architect Model</div>
              {architectModel?<div style={{display:"flex",gap:6,alignItems:"center"}}>
                <div style={{flex:1}}><ModelPicker value={architectModel} onChange={setArchitectModel} models={models} modelDetails={modelDetails} t={t} font={font}/></div>
                <button onClick={()=>setArchitectModel("")} style={{padding:"6px 10px",background:`${t.err}18`,border:`1px solid ${t.err}33`,borderRadius:7,color:t.err,fontSize:10,cursor:"pointer",fontWeight:600,whiteSpace:"nowrap"}}>Reset</button>
              </div>:<div onClick={()=>setArchitectModel(models[0]||"")} style={{display:"flex",alignItems:"center",gap:6,padding:"5px 10px",background:t.bgDeep,border:`1px dashed ${t.brd}55`,borderRadius:8,cursor:"pointer",transition:"all .15s"}}>
                <span style={{fontSize:14}}>📐</span>
                <div style={{flex:1}}><div style={{fontSize:11,fontWeight:600,color:t.mut}}>Inherits from Planning Model</div><div style={{fontSize:9,color:t.dim}}>Click to override for the Architect agent only</div></div>
                <span style={{fontSize:9,color:t.mut}}>▾</span>
              </div>}
            </div>

            {/* Reviewer — inherits from Planning Model */}
            <div style={{marginBottom:14}}>
              <div style={{fontSize:12,color:t.dim,marginBottom:6,fontWeight:600}}>🔍 Reviewer Model</div>
              {reviewerModel?<div style={{display:"flex",gap:6,alignItems:"center"}}>
                <div style={{flex:1}}><ModelPicker value={reviewerModel} onChange={setReviewerModel} models={models} modelDetails={modelDetails} t={t} font={font}/></div>
                <button onClick={()=>setReviewerModel("")} style={{padding:"6px 10px",background:`${t.err}18`,border:`1px solid ${t.err}33`,borderRadius:7,color:t.err,fontSize:10,cursor:"pointer",fontWeight:600,whiteSpace:"nowrap"}}>Reset</button>
              </div>:<div onClick={()=>setReviewerModel(models[0]||"")} style={{display:"flex",alignItems:"center",gap:6,padding:"5px 10px",background:t.bgDeep,border:`1px dashed ${t.brd}55`,borderRadius:8,cursor:"pointer",transition:"all .15s"}}>
                <span style={{fontSize:14}}>🔍</span>
                <div style={{flex:1}}><div style={{fontSize:11,fontWeight:600,color:t.mut}}>Inherits from Planning Model</div><div style={{fontSize:9,color:t.dim}}>Click to override for the Reviewer agent only</div></div>
                <span style={{fontSize:9,color:t.mut}}>▾</span>
              </div>}
            </div>

            {/* Acceptance — inherits from Planning Model */}
            <div style={{marginBottom:14}}>
              <div style={{fontSize:12,color:t.dim,marginBottom:6,fontWeight:600}}>✅ Acceptance Model</div>
              {acceptanceModel?<div style={{display:"flex",gap:6,alignItems:"center"}}>
                <div style={{flex:1}}><ModelPicker value={acceptanceModel} onChange={setAcceptanceModel} models={models} modelDetails={modelDetails} t={t} font={font}/></div>
                <button onClick={()=>setAcceptanceModel("")} style={{padding:"6px 10px",background:`${t.err}18`,border:`1px solid ${t.err}33`,borderRadius:7,color:t.err,fontSize:10,cursor:"pointer",fontWeight:600,whiteSpace:"nowrap"}}>Reset</button>
              </div>:<div onClick={()=>setAcceptanceModel(models[0]||"")} style={{display:"flex",alignItems:"center",gap:6,padding:"5px 10px",background:t.bgDeep,border:`1px dashed ${t.brd}55`,borderRadius:8,cursor:"pointer",transition:"all .15s"}}>
                <span style={{fontSize:14}}>✅</span>
                <div style={{flex:1}}><div style={{fontSize:11,fontWeight:600,color:t.mut}}>Inherits from Planning Model</div><div style={{fontSize:9,color:t.dim}}>Click to override for the Acceptance agent only</div></div>
                <span style={{fontSize:9,color:t.mut}}>▾</span>
              </div>}
            </div>

            {/* Builder — inherits from Coder Model */}
            <div style={{marginBottom:14}}>
              <div style={{fontSize:12,color:t.dim,marginBottom:6,fontWeight:600}}>🏗 Builder Model</div>
              {builderModel?<div style={{display:"flex",gap:6,alignItems:"center"}}>
                <div style={{flex:1}}><ModelPicker value={builderModel} onChange={setBuilderModel} models={models} modelDetails={modelDetails} t={t} font={font}/></div>
                <button onClick={()=>setBuilderModel("")} style={{padding:"6px 10px",background:`${t.err}18`,border:`1px solid ${t.err}33`,borderRadius:7,color:t.err,fontSize:10,cursor:"pointer",fontWeight:600,whiteSpace:"nowrap"}}>Reset</button>
              </div>:<div onClick={()=>setBuilderModel(models[0]||"")} style={{display:"flex",alignItems:"center",gap:6,padding:"5px 10px",background:t.bgDeep,border:`1px dashed ${t.brd}55`,borderRadius:8,cursor:"pointer",transition:"all .15s"}}>
                <span style={{fontSize:14}}>🏗</span>
                <div style={{flex:1}}><div style={{fontSize:11,fontWeight:600,color:t.mut}}>Inherits from Coder Model</div><div style={{fontSize:9,color:t.dim}}>Click to override the Builder (OpenHands) agent only</div></div>
                <span style={{fontSize:9,color:t.mut}}>▾</span>
              </div>}
            </div>

            {/* Fixer — inherits from Coder Model */}
            <div style={{marginBottom:14}}>
              <div style={{fontSize:12,color:t.dim,marginBottom:6,fontWeight:600}}>🛠 Fixer Model</div>
              {fixerModel?<div style={{display:"flex",gap:6,alignItems:"center"}}>
                <div style={{flex:1}}><ModelPicker value={fixerModel} onChange={setFixerModel} models={models} modelDetails={modelDetails} t={t} font={font}/></div>
                <button onClick={()=>setFixerModel("")} style={{padding:"6px 10px",background:`${t.err}18`,border:`1px solid ${t.err}33`,borderRadius:7,color:t.err,fontSize:10,cursor:"pointer",fontWeight:600,whiteSpace:"nowrap"}}>Reset</button>
              </div>:<div onClick={()=>setFixerModel(models[0]||"")} style={{display:"flex",alignItems:"center",gap:6,padding:"5px 10px",background:t.bgDeep,border:`1px dashed ${t.brd}55`,borderRadius:8,cursor:"pointer",transition:"all .15s"}}>
                <span style={{fontSize:14}}>🛠</span>
                <div style={{flex:1}}><div style={{fontSize:11,fontWeight:600,color:t.mut}}>Inherits from Coder Model</div><div style={{fontSize:9,color:t.dim}}>Click to override the Fixer agent only</div></div>
                <span style={{fontSize:9,color:t.mut}}>▾</span>
              </div>}
            </div>

            {/* ProjectQA — inherits from chat/profile model */}
            <div style={{marginBottom:6}}>
              <div style={{fontSize:12,color:t.dim,marginBottom:6,fontWeight:600}}>❓ ProjectQA Model</div>
              {qaModel?<div style={{display:"flex",gap:6,alignItems:"center"}}>
                <div style={{flex:1}}><ModelPicker value={qaModel} onChange={setQaModel} models={models} modelDetails={modelDetails} t={t} font={font}/></div>
                <button onClick={()=>setQaModel("")} style={{padding:"6px 10px",background:`${t.err}18`,border:`1px solid ${t.err}33`,borderRadius:7,color:t.err,fontSize:10,cursor:"pointer",fontWeight:600,whiteSpace:"nowrap"}}>Reset</button>
              </div>:<div onClick={()=>setQaModel(models[0]||"")} style={{display:"flex",alignItems:"center",gap:6,padding:"5px 10px",background:t.bgDeep,border:`1px dashed ${t.brd}55`,borderRadius:8,cursor:"pointer",transition:"all .15s"}}>
                <span style={{fontSize:14}}>❓</span>
                <div style={{flex:1}}><div style={{fontSize:11,fontWeight:600,color:t.mut}}>Inherits from chat model</div><div style={{fontSize:9,color:t.dim}}>Click to override the ProjectQA agent only</div></div>
                <span style={{fontSize:9,color:t.mut}}>▾</span>
              </div>}
            </div>
          </div>}
        </div>

        <div style={{borderTop:`1px solid ${t.brd}22`,paddingTop:14,marginBottom:14,display:"flex",alignItems:"center",gap:10}}>
          <label style={{fontSize:12,color:t.dim,fontWeight:600,whiteSpace:"nowrap"}}>OpenHands Enabled</label>
          <div onClick={()=>setOpenhandsEnabled(!openhandsEnabled)} style={{width:36,height:20,borderRadius:10,background:openhandsEnabled?t.acc:`${t.mut}44`,cursor:"pointer",position:"relative",transition:"all .2s"}}>
            <div style={{width:16,height:16,borderRadius:8,background:t.text,position:"absolute",top:2,left:openhandsEnabled?18:2,transition:"all .2s"}}/>
          </div>
          <span style={{fontSize:10,color:t.mut}}>{openhandsEnabled?"On":"Off"}</span>
        </div>

        <div style={{borderTop:`1px solid ${t.brd}22`,paddingTop:14,marginBottom:14}}>
          <div style={{display:"flex",alignItems:"center",gap:10,marginBottom:10}}>
            <label style={{fontSize:12,color:t.dim,fontWeight:600,whiteSpace:"nowrap"}}>Aider Enabled</label>
            <div onClick={()=>setAiderEnabled(!aiderEnabled)} style={{width:36,height:20,borderRadius:10,background:aiderEnabled?t.acc:`${t.mut}44`,cursor:"pointer",position:"relative",transition:"all .2s"}}>
              <div style={{width:16,height:16,borderRadius:8,background:t.text,position:"absolute",top:2,left:aiderEnabled?18:2,transition:"all .2s"}}/>
            </div>
            <span style={{fontSize:10,color:t.mut}}>{aiderEnabled?"Uploaded-project fixes use Aider":"Fallback Fixer"}</span>
          </div>
          <div style={{fontSize:12,color:t.dim,marginBottom:8,fontWeight:600}}>Aider/Fix Model</div>
          {aiderModel?<div style={{display:"flex",gap:6,alignItems:"center"}}>
            <div style={{flex:1}}><ModelPicker value={aiderModel} onChange={setAiderModel} models={models} modelDetails={modelDetails} t={t} font={font}/></div>
            <button onClick={()=>setAiderModel("")} style={{padding:"6px 10px",background:`${t.err}18`,border:`1px solid ${t.err}33`,borderRadius:7,color:t.err,fontSize:10,cursor:"pointer",fontWeight:600,whiteSpace:"nowrap"}}>Reset</button>
          </div>:<div onClick={()=>setAiderModel(fixerModel||coderModel||models[0]||"")} style={{display:"flex",alignItems:"center",gap:6,padding:"5px 10px",background:t.bgDeep,border:`1px dashed ${t.brd}55`,borderRadius:8,cursor:"pointer",transition:"all .15s"}}>
            <span style={{fontSize:14}}>🛠</span>
            <div style={{flex:1}}><div style={{fontSize:11,fontWeight:600,color:t.mut}}>Inherits from Fixer/Coder Model</div><div style={{fontSize:9,color:t.dim}}>Click to pin the Aider edit model</div></div>
            <span style={{fontSize:9,color:t.mut}}>▾</span>
          </div>}
          <label style={{display:"flex",alignItems:"center",gap:8,marginTop:10,fontSize:11,color:t.mut,cursor:"pointer"}}>
            <input type="checkbox" checked={aiderAutoTest} onChange={e=>setAiderAutoTest(e.target.checked)} style={{accentColor:t.acc}}/>
            Auto-run safe test command after Aider edits
          </label>
        </div>

        <div style={{marginBottom:14}}>
          <label style={{fontSize:12,color:t.dim,fontWeight:600,display:"block",marginBottom:6}}>Daedalus Context Window: <span style={{color:t.acc}}>{coderNumCtx.toLocaleString()}</span></label>
          <input type="range" min="2048" max="262144" step="2048" value={coderNumCtx} onChange={e=>setCoderNumCtx(parseInt(e.target.value))} style={{width:"100%",accentColor:t.acc}}/>
          <div style={{display:"flex",justifyContent:"space-between",fontSize:9,color:t.mut}}><span>2048</span><span>262144</span></div>
          <div style={{fontSize:9,color:t.mut,marginTop:4,lineHeight:1.5}}>
            {coderNumCtx<=16384?<><b style={{color:t.acc}}>Compact (≤16K)</b> — small tasks, single-function edits. Fits comfortably with 3 models warm.</>
            :coderNumCtx<=65536?<><b style={{color:t.acc}}>Recommended (32–64K)</b> — sweet spot for most Daedalus work. Multi-file refactors fit, VRAM stays comfortable.</>
            :coderNumCtx<=131072?<><b style={{color:t.acc}}>Large (64–128K)</b> — whole-file analysis, big refactors. Keep only 1–2 models warm or you may hit VRAM limits.</>
            :<><b style={{color:t.err}}>Maximum (128–256K)</b> — whole-codebase context. Drop <code>OLLAMA_MAX_LOADED_MODELS</code> to 1 or expect OOM. First run after slider change reloads the model.</>}
          </div>
        </div>

        <div style={{marginBottom:14}}>
          <label style={{fontSize:12,color:t.dim,fontWeight:600,display:"block",marginBottom:6}}>Max Agent Rounds: <span style={{color:t.acc}}>{openhandsMaxRounds}</span></label>
          <input type="range" min="5" max="40" step="1" value={openhandsMaxRounds} onChange={e=>setOpenhandsMaxRounds(parseInt(e.target.value))} style={{width:"100%",accentColor:t.acc}}/>
          <div style={{display:"flex",justifyContent:"space-between",fontSize:9,color:t.mut}}><span>5</span><span>40</span></div>
        </div>

        <div style={{marginBottom:6}}>
          <label style={{fontSize:12,color:t.dim,fontWeight:600,display:"block",marginBottom:6}}>Reasoning Effort: <span style={{color:t.acc}}>{openhandsReasoningEffort}</span></label>
          <select value={openhandsReasoningEffort} onChange={e=>setOpenhandsReasoningEffort(e.target.value)} style={{width:"100%",background:t.bgDeep,border:`1px solid ${t.brd}44`,color:t.text,padding:"8px 12px",borderRadius:8,fontFamily:font,fontSize:12,outline:"none",cursor:"pointer"}}>
            <option value="low">Low — fastest, minimal think tokens</option>
            <option value="medium">Medium — balanced (recommended for local Ollama)</option>
            <option value="high">High — most thorough, slow on local models</option>
          </select>
          <div style={{fontSize:9,color:t.mut,marginTop:4}}>Controls how much the Builder LLM thinks before each tool call. Drop to <b>low</b> if generate_code is taking minutes per file.</div>
        </div>

        <div style={{marginBottom:6,marginTop:12}}>
          <label style={{display:"flex",alignItems:"center",gap:8,fontSize:12,color:t.dim,fontWeight:600,cursor:"pointer"}}>
            <input type="checkbox" checked={openhandsDisableThinking} onChange={e=>setOpenhandsDisableThinking(e.target.checked)} style={{accentColor:t.acc}}/>
            Disable model thinking during builds
          </label>
          <div style={{fontSize:9,color:t.mut,marginTop:4}}>Sends <b>think: false</b> to thinking-capable coder models (qwen3.5-class). Without it they reason with an unbounded budget before every tool call, making builds slow and tool calls flaky. Non-thinking models are unaffected.</div>
        </div>
      </div>

      {/* TILE: Appearance */}
      <div style={{...settingsCardS,display:settingsTab==="appearance"?"block":"none"}}>
        <div style={{fontSize:14,fontWeight:800,color:t.acc,letterSpacing:.5,marginBottom:4,display:"flex",alignItems:"center",gap:8}}>🎨 Appearance</div>
        <div style={{...settingsKickerS,marginBottom:14}}>Theme, background animation, font, and density preferences.</div>
        <div style={{marginBottom:12}}>
          {(()=>{
            const previewKey=pendingTheme||tm;
            const pTh=THEMES[previewKey]||t;
            return <div>
              <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(170px,1fr))",gap:8,alignItems:"end",marginBottom:8}}>
                <div>
                  <div style={{fontSize:12,color:t.dim,marginBottom:6,fontWeight:600}}>Theme</div>
                  <select value={previewKey} onChange={e=>setPendingTheme(e.target.value)} style={{width:"100%",background:t.bgDeep,border:`1px solid ${t.brd}44`,color:t.text,padding:"8px 12px",borderRadius:8,fontFamily:font,fontSize:12,outline:"none",cursor:"pointer"}}>
                    {Object.entries(THEMES).map(([k,th])=><option key={k} value={k}>{th.name}</option>)}
                  </select>
                </div>
                <div>
                  <div style={{fontSize:12,color:t.dim,marginBottom:6,fontWeight:600}}>Background</div>
                  <select value={bgEffect} onChange={e=>{setBgEffect(e.target.value);flashSettingsPulse("Saved locally","success");}} style={{width:"100%",background:t.bgDeep,border:`1px solid ${t.brd}44`,color:t.text,padding:"8px 12px",borderRadius:8,fontFamily:font,fontSize:12,outline:"none",cursor:"pointer"}}>
                    {BACKGROUND_EFFECTS.map(e=><option key={e.id} value={e.id}>{e.name}</option>)}
                  </select>
                </div>
              </div>
              <div style={{padding:"10px 14px",background:pTh.bgDeep,border:`1px solid ${pTh.brd}44`,borderRadius:10,marginBottom:8}}>
                <div style={{display:"flex",gap:4,marginBottom:8,flexWrap:"wrap"}}>
                  {[["bg",pTh.bg],["surface",pTh.surface],["text",pTh.text],["accent",pTh.acc],["warm",pTh.warm],["ok",pTh.ok],["err",pTh.err],["pink",pTh.pink]].map(([label,c])=><div key={label} style={{display:"flex",flexDirection:"column",alignItems:"center",gap:2}}>
                    <div style={{width:18,height:18,borderRadius:4,background:c,border:`1px solid ${pTh.brd}66`}}/>
                    <span style={{fontSize:7,color:pTh.mut}}>{label}</span>
                  </div>)}
                </div>
                <div style={{borderRadius:8,overflow:"hidden",background:pTh.bg,padding:8}}>
                  <div style={{display:"flex",gap:6,marginBottom:4}}>
                    <div style={{flex:1,padding:"6px 10px",borderRadius:8,background:`${pTh.surface}CC`,fontSize:10,color:pTh.text}}>Hello!</div>
                  </div>
                  <div style={{display:"flex",gap:6,justifyContent:"flex-end"}}>
                    <div style={{flex:1,maxWidth:"70%",padding:"6px 10px",borderRadius:8,background:`${pTh.acc}22`,border:`1px solid ${pTh.acc}33`,fontSize:10,color:pTh.acc}}>Response preview</div>
                  </div>
                </div>
              </div>
              <div style={{display:"flex",gap:6}}>
                {pendingTheme&&pendingTheme!==tm&&<><button onClick={()=>{setTm(pendingTheme);setPendingTheme(null);flashSettingsPulse("Saved locally","success");}} style={{flex:1,padding:"6px 12px",borderRadius:7,background:`${t.ok}22`,border:`1px solid ${t.ok}44`,color:t.ok,cursor:"pointer",fontFamily:font,fontSize:12,fontWeight:600}}>Apply</button>
                <button onClick={()=>setPendingTheme(null)} style={{padding:"6px 12px",borderRadius:7,background:t.bgDeep,border:`1px solid ${t.brd}44`,color:t.dim,cursor:"pointer",fontFamily:font,fontSize:12}}>Cancel</button></>}
              </div>
            </div>;
          })()}
        </div>
        <div style={{marginBottom:12}}>
          <div style={{fontSize:12,color:t.dim,marginBottom:6,fontWeight:600}}>Font</div>
          {(()=>{
            const previewIdx=pendingFont!==null?pendingFont:fi;
            const pF=FONTS[previewIdx]||FONTS[0];
            return <div>
              <select value={previewIdx} onChange={e=>setPendingFont(parseInt(e.target.value))} style={{width:"100%",background:t.bgDeep,border:`1px solid ${t.brd}44`,color:t.text,padding:"8px 12px",borderRadius:8,fontFamily:pF.v,fontSize:12,outline:"none",cursor:"pointer",marginBottom:8}}>
                {FONTS.map((f,i)=><option key={i} value={i} style={{fontFamily:f.v}}>{f.name}</option>)}
              </select>
              <div style={{padding:"10px 14px",background:t.bgDeep,border:`1px solid ${t.brd}44`,borderRadius:10,marginBottom:8}}>
                <div style={{fontFamily:pF.v,fontSize:14,color:t.text,marginBottom:6,lineHeight:1.6}}>The quick brown fox jumps over the lazy dog.</div>
                <div style={{fontFamily:pF.v,fontSize:11,color:t.dim,lineHeight:1.5}}>{"const x = () => { return 42; };"}</div>
                <div style={{display:"flex",gap:12,marginTop:8}}>
                  <div style={{display:"flex",alignItems:"center",gap:6}}>
                    <span style={{fontSize:10,color:t.mut}}>UI Size</span>
                    <select value={uiFontSize} onChange={e=>{setUiFontSize(parseInt(e.target.value));flashSettingsPulse("Saved locally","success");}} style={{background:t.bgDeep,border:`1px solid ${t.brd}44`,color:t.text,padding:"4px 8px",borderRadius:6,fontFamily:pF.v,fontSize:11,outline:"none",cursor:"pointer"}}>
                      {[10,11,12,13,14,15,16,17,18,19,20,22,24].map(v=><option key={v} value={v}>{v}px</option>)}
                    </select>
                  </div>
                  <div style={{display:"flex",alignItems:"center",gap:6}}>
                    <span style={{fontSize:10,color:t.mut}}>Chat Size</span>
                    <select value={fontSize} onChange={e=>{setFontSize(parseInt(e.target.value));flashSettingsPulse("Saved locally","success");}} style={{background:t.bgDeep,border:`1px solid ${t.brd}44`,color:t.text,padding:"4px 8px",borderRadius:6,fontFamily:pF.v,fontSize:11,outline:"none",cursor:"pointer"}}>
                      {[11,12,13,14,15,16,17,18,19,20,22,24].map(v=><option key={v} value={v}>{v}px</option>)}
                    </select>
                  </div>
                </div>
              </div>
              <div style={{display:"flex",gap:6}}>
                {pendingFont!==null&&pendingFont!==fi&&<><button onClick={()=>{setFi(pendingFont);setPendingFont(null);flashSettingsPulse("Saved locally","success");}} style={{flex:1,padding:"6px 12px",borderRadius:7,background:`${t.ok}22`,border:`1px solid ${t.ok}44`,color:t.ok,cursor:"pointer",fontFamily:font,fontSize:12,fontWeight:600}}>Apply</button>
                <button onClick={()=>setPendingFont(null)} style={{padding:"6px 12px",borderRadius:7,background:t.bgDeep,border:`1px solid ${t.brd}44`,color:t.dim,cursor:"pointer",fontFamily:font,fontSize:12}}>Cancel</button></>}
              </div>
            </div>;
          })()}
        </div>
        <div style={{marginBottom:12}}>
          <div style={{fontSize:12,color:t.dim,marginBottom:6,fontWeight:600}}>Chat Width</div>
          <div style={{display:"flex",flexWrap:"wrap",gap:4}}>
            {[560,640,720,800,880,960,1040,1120,1200].map(v=><button key={v} onClick={()=>{setChatWidth(v);flashSettingsPulse("Saved locally","success");}} style={{padding:"5px 10px",borderRadius:7,border:`1px solid ${chatWidth===v?t.acc:t.brd}44`,background:chatWidth===v?`${t.acc}22`:t.bgDeep,color:chatWidth===v?t.acc:t.dim,fontFamily:font,fontSize:12,cursor:"pointer",fontWeight:chatWidth===v?700:400}}>{v}</button>)}
          </div>
        </div>
        <div style={{display:"flex",gap:8,flexWrap:"wrap"}}>
          {[
            [compactMode,setCompactMode,"Compact"],
            [autoExpandThink,setAutoExpandThink,"Auto-think"],
            [showNavLabels,setShowNavLabels,"Nav Labels"],
            [autoTitle,setAutoTitle,"Auto Title"],
          ].map(([val,setter,label],idx)=><div key={idx} onClick={()=>{setter(!val);flashSettingsPulse("Saved locally","success");}} style={{display:"flex",alignItems:"center",gap:6,padding:"6px 12px",background:`${t.bgDeep}`,borderRadius:7,border:`1px solid ${val?t.acc:t.brd}33`,cursor:"pointer",fontSize:12}}>
            <span style={{color:val?t.acc:t.mut,fontSize:14}}>{val?<IC.ToggleOn/>:<IC.ToggleOff/>}</span>
            <span style={{color:val?t.acc:t.dim,fontWeight:600}}>{label}</span>
          </div>)}
        </div>
      </div>

      {/* TILE: RAG Pipeline */}
      <div style={{...settingsCardS,display:settingsTab==="rag"?"block":"none"}}>
        <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:14}}>
          <div style={{fontSize:14,fontWeight:800,color:t.acc,letterSpacing:.5,display:"flex",alignItems:"center",gap:8}}>🧠 RAG Pipeline</div>
          <div style={{display:"flex",gap:8}}>
            <button onClick={()=>setRagSettings({embed_model:"nomic-embed-text",chunk_size:500,chunk_overlap:50,top_k:6,max_context_chars:6000,research_top_k:4,research_max_chars:3000})} style={{...btnS(t.mut),fontSize:12,padding:"5px 12px"}}>Default</button>
            <button disabled={ragSaving} onClick={async()=>{
              setRagSaving(true);setRagSaveState("saving");
              try{
                const r=await fetch(`${API}/api/settings`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({rag:ragSettings})});
                if(!r.ok)throw new Error(`HTTP ${r.status}`);
                setRagSaveState("saved");notify({type:"success",text:"RAG settings saved"});
                setTimeout(()=>setRagSaveState(null),2200);
              }catch(e){setRagSaveState("failed");notify({type:"error",text:"RAG settings failed",detail:e.message});}
              setRagSaving(false);
            }} style={{...btnS(t.ok),opacity:ragSaving?0.5:1,fontSize:12,padding:"5px 12px"}}>
              {ragSaving?"Saving...":"Save"}
            </button>
            <button onClick={async()=>{
              const ok=await confirmAction({title:"Reindex knowledge bases",body:"Reindex all knowledge bases now? Existing chunks will be refreshed and this can take a while on large files.",confirmLabel:"Reindex",tone:"warning"});
              if(!ok)return;
              const activityId=addActivity({name:"RAG reindex",kind:"rag",message:"Reindexing knowledge bases...",pct:20});
              try{
                const r=await fetch(`${API}/api/knowledge-bases/reindex-all`,{method:"POST"});
                const d=await r.json().catch(()=>({}));
                if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
                updateActivity(activityId,{status:"done",message:`${d.kbs?.length||0} KBs reindexed`,pct:100});
                notify({type:"success",text:"Reindex complete",detail:`${d.kbs?.length||0} knowledge bases reindexed.`});
              }catch(e){updateActivity(activityId,{status:"error",error:e.message,message:"Reindex failed"});notify({type:"error",text:"Reindex failed",detail:e.message});}
              fetch(`${API}/api/rag/stats`).then(r=>r.json()).then(setRagStats).catch(()=>{});
            }} style={{...btnS(t.f1),fontSize:12,padding:"5px 12px"}}>🔄 Reindex</button>
          </div>
        </div>
        {ragSaveState&&<div style={{fontSize:10,color:ragSaveState==="failed"?t.err:ragSaveState==="saved"?t.ok:t.mut,marginTop:-8,marginBottom:10,textAlign:"right"}}>{ragSaveState==="saving"?"Saving...":ragSaveState==="saved"?"Saved":"Failed"}</div>}

        {/* Embedding Model */}
        <div style={{marginBottom:14}}>
          <div style={{fontSize:11,color:t.mut,marginBottom:5,fontWeight:600,textTransform:"uppercase",letterSpacing:.5,display:"flex",alignItems:"center",gap:4}}>Embedding Model <Tip k="embed_model" t={t}/></div>
          <input value={ragSettings.embed_model} onChange={e=>setRagSettings(p=>({...p,embed_model:e.target.value}))} placeholder="nomic-embed-text" style={{...inputS,fontFamily:"monospace",fontSize:13,width:"100%",padding:"8px 12px"}}/>
        </div>

        {/* Chunking Preview */}
        <div style={{background:t.bgDeep,borderRadius:10,border:`1px solid ${t.brd}22`,padding:"12px 14px",marginBottom:14}}>
          <div style={{fontSize:11,fontWeight:700,color:t.warm,textTransform:"uppercase",letterSpacing:.5,marginBottom:10,display:"flex",alignItems:"center",gap:6}}>📐 Chunking</div>
          <div style={{display:"flex",gap:16,marginBottom:12}}>
            <div style={{flex:1}}>
              <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:4}}>
                <span style={{fontSize:10,color:t.dim,fontWeight:600}}>Chunk Size <Tip k="chunk_size" t={t}/></span>
                <span style={{fontSize:12,fontWeight:700,color:t.acc,fontFamily:"monospace"}}>{ragSettings.chunk_size}</span>
              </div>
              <input type="range" min={128} max={2000} step={64} value={ragSettings.chunk_size} onChange={e=>setRagSettings(p=>({...p,chunk_size:parseInt(e.target.value)}))} style={{width:"100%",accentColor:t.acc}}/>
              <div style={{display:"flex",justifyContent:"space-between",fontSize:9,color:t.mut,marginTop:2}}><span>128</span><span>2000</span></div>
            </div>
            <div style={{flex:1}}>
              <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:4}}>
                <span style={{fontSize:10,color:t.dim,fontWeight:600}}>Overlap <Tip k="chunk_overlap" t={t}/></span>
                <span style={{fontSize:12,fontWeight:700,color:t.acc,fontFamily:"monospace"}}>{ragSettings.chunk_overlap}</span>
              </div>
              <input type="range" min={0} max={200} step={10} value={ragSettings.chunk_overlap} onChange={e=>setRagSettings(p=>({...p,chunk_overlap:parseInt(e.target.value)}))} style={{width:"100%",accentColor:t.acc}}/>
              <div style={{display:"flex",justifyContent:"space-between",fontSize:9,color:t.mut,marginTop:2}}><span>0</span><span>200</span></div>
            </div>
          </div>
          {/* Visual chunk preview */}
          <div style={{background:`${t.surface}44`,borderRadius:8,padding:"8px 10px",border:`1px solid ${t.brd}15`}}>
            <div style={{fontSize:9,color:t.mut,marginBottom:6,fontWeight:600,textTransform:"uppercase",letterSpacing:.5}}>Preview</div>
            <div style={{display:"flex",gap:2,alignItems:"center",height:22}}>
              {[0,1,2,3,4].map(i=>{
                const w=Math.max(20,Math.min(80,ragSettings.chunk_size/25));
                const ov=Math.max(0,Math.min(w-4,ragSettings.chunk_overlap/25));
                const colors=[t.acc,t.warm,t.f1,t.ok,t.pink];
                return <React.Fragment key={i}>
                  {i>0&&ov>0&&<div style={{width:ov,height:18,background:`${t.text}18`,borderRadius:2,marginLeft:-ov,marginRight:0,zIndex:1,border:`1px dashed ${t.mut}44`}} title={`${ragSettings.chunk_overlap} char overlap`}/>}
                  <div style={{width:w,height:18,background:`${colors[i]}22`,border:`1px solid ${colors[i]}44`,borderRadius:3,display:"flex",alignItems:"center",justifyContent:"center",fontSize:8,color:colors[i],fontWeight:700,flexShrink:0}}>{ragSettings.chunk_size}</div>
                </React.Fragment>;
              })}
              <span style={{fontSize:9,color:t.mut,marginLeft:4}}>...</span>
            </div>
          </div>
        </div>

        {/* Retrieval Settings */}
        <div style={{background:t.bgDeep,borderRadius:10,border:`1px solid ${t.brd}22`,padding:"12px 14px",marginBottom:14}}>
          <div style={{fontSize:11,fontWeight:700,color:t.f1,textTransform:"uppercase",letterSpacing:.5,marginBottom:10,display:"flex",alignItems:"center",gap:6}}>🔍 Retrieval</div>
          <div style={{display:"flex",gap:16,marginBottom:12}}>
            <div style={{flex:1}}>
              <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:4}}>
                <span style={{fontSize:10,color:t.dim,fontWeight:600}}>Max Context <Tip k="max_context" t={t}/></span>
                <span style={{fontSize:12,fontWeight:700,color:t.f1,fontFamily:"monospace"}}>{ragSettings.max_context_chars>=1000?`${(ragSettings.max_context_chars/1000).toFixed(ragSettings.max_context_chars%1000?1:0)}K`:ragSettings.max_context_chars}</span>
              </div>
              <input type="range" min={1000} max={20000} step={500} value={ragSettings.max_context_chars} onChange={e=>setRagSettings(p=>({...p,max_context_chars:parseInt(e.target.value)}))} style={{width:"100%",accentColor:t.f1}}/>
              <div style={{display:"flex",justifyContent:"space-between",fontSize:9,color:t.mut,marginTop:2}}><span>1K</span><span>20K</span></div>
            </div>
          </div>
          <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:14}}>
            <div>
              <div style={{fontSize:10,color:t.dim,marginBottom:6,fontWeight:600}}>KB Retrieval (top-K) <Tip k="top_k" t={t}/></div>
              <div style={{display:"flex",gap:3}}>
                {[3,4,6,8,10,12].map(v=><button key={v} onClick={()=>setRagSettings(p=>({...p,top_k:v}))} style={{flex:1,padding:"6px 0",borderRadius:6,border:`1px solid ${ragSettings.top_k===v?t.f1:t.brd}33`,background:ragSettings.top_k===v?`${t.f1}22`:t.bgDeep,color:ragSettings.top_k===v?t.f1:t.dim,fontSize:12,cursor:"pointer",fontFamily:font,fontWeight:ragSettings.top_k===v?700:400,transition:"all .15s"}}>{v}</button>)}
              </div>
            </div>
            <div>
              <div style={{fontSize:10,color:t.dim,marginBottom:6,fontWeight:600}}>Research Memory (top-K) <Tip k="research_top_k" t={t}/></div>
              <div style={{display:"flex",gap:3}}>
                {[2,4,6,8,10].map(v=><button key={v} onClick={()=>setRagSettings(p=>({...p,research_top_k:v}))} style={{flex:1,padding:"6px 0",borderRadius:6,border:`1px solid ${ragSettings.research_top_k===v?t.pink:t.brd}33`,background:ragSettings.research_top_k===v?`${t.pink}22`:t.bgDeep,color:ragSettings.research_top_k===v?t.pink:t.dim,fontSize:12,cursor:"pointer",fontFamily:font,fontWeight:ragSettings.research_top_k===v?700:400,transition:"all .15s"}}>{v}</button>)}
              </div>
            </div>
          </div>
        </div>

        {/* Stats */}
        {ragStats&&<div style={{background:t.bgDeep,borderRadius:10,border:`1px solid ${t.brd}22`,padding:"12px 14px"}}>
          <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:10}}>
            <span style={{fontSize:11,fontWeight:700,color:t.ok,textTransform:"uppercase",letterSpacing:.5}}>📊 Index Stats</span>
            <div style={{flex:1}}/>
            {ragStats.collections&&ragStats.collections.length>0&&<button onClick={async()=>{
              const ok=await confirmAction({title:"Purge RAG collections",body:`Delete all ${ragStats.collections.length} RAG collections? This removes indexed chunks. You can reindex later.`,confirmLabel:"Purge Collections",tone:"danger"});
              if(!ok)return;
              const activityId=addActivity({name:"RAG purge",kind:"cleanup",message:"Deleting indexed collections...",pct:30});
              try{const r=await fetch(`${API}/api/rag/collections`,{method:"DELETE"});const d=await r.json();
                if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
                setRagStats(p=>({...p,total_collections:0,total_chunks:0,collections:[]}));
                updateActivity(activityId,{status:"done",message:`Purged ${d.deleted} collections`,pct:100});
                notify({type:"success",text:"RAG collections purged",detail:`Deleted ${d.deleted} collections.`});
              }catch(e){updateActivity(activityId,{status:"error",error:e.message,message:"Purge failed"});notify({type:"error",text:"Purge failed",detail:e.message});}
            }} style={{...btnS(t.err),padding:"3px 10px",fontSize:9}}>🗑 Purge</button>}
          </div>
          <div style={{display:"flex",gap:0,marginBottom:ragStats.collections?.length?10:0}}>
            {[
              {label:"Collections",value:ragStats.total_collections||0,color:t.acc},
              {label:"Chunks",value:ragStats.total_chunks||0,color:t.warm},
              {label:"Disk",value:ragStats.disk_usage||"—",color:t.ok}
            ].map((s,i)=><div key={i} style={{flex:1,textAlign:"center",padding:"6px 0",borderRight:i<2?`1px solid ${t.brd}22`:"none"}}>
              <div style={{fontSize:22,fontWeight:800,color:s.color,lineHeight:1.2}}>{s.value}</div>
              <div style={{fontSize:9,color:t.mut,textTransform:"uppercase",letterSpacing:.5,marginTop:2}}>{s.label}</div>
            </div>)}
          </div>
          {ragStats.collections&&ragStats.collections.length>0&&<div style={{display:"flex",flexWrap:"wrap",gap:4}}>
            {ragStats.collections.map((c,i)=>{
              const isRes=c.name.startsWith("research-");const col=isRes?t.pink:t.acc;
              return <span key={i} style={{display:"inline-flex",alignItems:"center",gap:4,padding:"3px 8px",borderRadius:6,background:`${col}10`,border:`1px solid ${col}20`,fontSize:10,color:col,transition:"all .15s"}}
                onMouseOver={e=>{e.currentTarget.style.background=`${col}22`;e.currentTarget.style.borderColor=`${col}44`;}}
                onMouseOut={e=>{e.currentTarget.style.background=`${col}10`;e.currentTarget.style.borderColor=`${col}20`;}}>
                {isRes?"🧠":"📚"} {c.name.replace("research-","").slice(0,20)} <span style={{fontWeight:700,color:t.mut}}>{c.count}</span>
              </span>;})}
          </div>}
        </div>}
      </div>

      {/* TILE: Loading Quotes */}
      <div style={{...settingsCardS,display:settingsTab==="quotes"?"block":"none"}}>
        <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",gap:12,marginBottom:12}}>
          <div>
            <div style={{fontSize:14,fontWeight:700,color:t.acc,letterSpacing:.5,display:"flex",alignItems:"center",gap:8}}>💬 Loading Quotes <span style={{fontSize:11,color:t.mut,fontWeight:400}}>({customQuotes.length})</span></div>
            <div style={{...settingsKickerS,marginTop:4}}>Shown on the startup loading screen. Edit, remove, or add quotes below.</div>
          </div>
          <button onClick={async()=>{const ok=await confirmAction({title:"Restore loading quotes",body:"Replace the current loading quotes with the built-in defaults?",confirmLabel:"Restore Defaults",tone:"warning"});if(!ok)return;setCustomQuotes(DEFAULT_LOADING_QUOTES);setEditQuoteIdx(null);notify({type:"success",text:"Loading quotes saved locally"});flashSettingsPulse("Saved locally","success");}} style={{...btnS(t.mut),fontSize:12,padding:"6px 12px",whiteSpace:"nowrap"}}>Restore Defaults</button>
        </div>
        <div style={{display:"flex",flexDirection:"column",gap:8,maxHeight:"min(48vh,460px)",overflowY:"auto",paddingRight:4,marginBottom:14}}>
          {customQuotes.map((q,i)=>editQuoteIdx===i?<div key={i} style={{background:t.bgDeep,border:`1px solid ${t.acc}33`,borderRadius:10,padding:12}}>
            <textarea value={editQuoteText} onChange={e=>setEditQuoteText(e.target.value)} style={{...inputS,minHeight:72,resize:"vertical",fontSize:12,lineHeight:1.5,marginBottom:8}}/>
            <input value={editQuoteAttr} onChange={e=>setEditQuoteAttr(e.target.value)} placeholder="Attribution" style={{...inputS,fontSize:12,padding:"8px 10px",marginBottom:10}}/>
            <div style={{display:"flex",gap:8,justifyContent:"flex-end"}}>
              <button onClick={()=>{setEditQuoteIdx(null);setEditQuoteText("");setEditQuoteAttr("");}} style={{...btnS(t.mut),fontSize:11}}>Cancel</button>
              <button onClick={()=>{if(!editQuoteText.trim())return;setCustomQuotes(p=>p.map((x,j)=>j===i?{t:editQuoteText.trim(),a:editQuoteAttr.trim()}:x));setEditQuoteIdx(null);setEditQuoteText("");setEditQuoteAttr("");notify({type:"success",text:"Loading quotes saved locally",duration:2500});flashSettingsPulse("Saved locally","success");}} style={{...btnS(t.ok),fontSize:11}}>Save</button>
            </div>
          </div>:<div key={i} style={{display:"grid",gridTemplateColumns:"minmax(0,1fr) auto",gap:12,alignItems:"start",background:t.bgDeep,border:`1px solid ${t.brd}22`,borderRadius:10,padding:"11px 12px"}}>
            <div style={{minWidth:0}}>
              <div style={{fontSize:13,color:t.text,lineHeight:1.55,fontStyle:"italic"}}>"{q.t}"</div>
              <div style={{fontSize:11,color:t.acc,marginTop:6,fontWeight:700}}>— {q.a||"Unknown"}</div>
            </div>
            <div style={{display:"flex",gap:6}}>
              <button onClick={()=>{setEditQuoteIdx(i);setEditQuoteText(q.t||"");setEditQuoteAttr(q.a||"");}} style={{...btnS(t.f1),padding:"5px 9px",fontSize:10}}>Edit</button>
              <button onClick={()=>{setCustomQuotes(p=>p.filter((_,j)=>j!==i));if(editQuoteIdx===i)setEditQuoteIdx(null);notify({type:"success",text:"Loading quotes saved locally",duration:2500});flashSettingsPulse("Saved locally","success");}} style={{...btnS(t.err),padding:"5px 9px",fontSize:10}}>Remove</button>
            </div>
          </div>)}
        </div>
        <div style={{display:"grid",gridTemplateColumns:"minmax(0,2fr) minmax(180px,1fr) auto",gap:8,alignItems:"center",borderTop:`1px solid ${t.brd}22`,paddingTop:12}}>
          <input value={newQuoteText} onChange={e=>setNewQuoteText(e.target.value)} placeholder='Quote text...' style={{...inputS,fontSize:12,padding:"8px 12px"}}/>
          <input value={newQuoteAttr} onChange={e=>setNewQuoteAttr(e.target.value)} placeholder="By" style={{...inputS,fontSize:12,padding:"8px 12px"}}
            onKeyDown={e=>{if(e.key==="Enter"&&newQuoteText.trim()){setCustomQuotes(p=>[...p,{t:newQuoteText.trim(),a:newQuoteAttr.trim()}]);setNewQuoteText("");setNewQuoteAttr("");notify({type:"success",text:"Loading quotes saved locally",duration:2500});flashSettingsPulse("Saved locally","success");}}}/>
          <button onClick={()=>{if(!newQuoteText.trim())return;setCustomQuotes(p=>[...p,{t:newQuoteText.trim(),a:newQuoteAttr.trim()}]);setNewQuoteText("");setNewQuoteAttr("");notify({type:"success",text:"Loading quotes saved locally",duration:2500});flashSettingsPulse("Saved locally","success");}}
            disabled={!newQuoteText.trim()} style={{...btnS(t.f1),opacity:newQuoteText.trim()?1:.4,flexShrink:0,fontSize:12,padding:"7px 14px",whiteSpace:"nowrap"}}>+ Add</button>
        </div>
      </div>

      {/* TILE: Sandbox & Cleanup */}
      <div style={{...settingsCardS,display:settingsTab==="cleanup"?"block":"none"}}>
        <div style={{fontSize:14,fontWeight:700,color:t.acc,letterSpacing:.5,marginBottom:10,display:"flex",alignItems:"center",gap:8}}>🗃️ Sandbox & Cleanup</div>
        <SandboxSection t={t} btnS={btnS} labelS={labelS} notify={notify} addActivity={addActivity} updateActivity={updateActivity}/>
      </div>

	      {/* TILE: Danger Zone */}
	      <div style={{display:settingsTab==="danger"?"block":"none",background:`${t.err}06`,border:`1px solid ${t.err}22`,borderRadius:12,padding:"16px 18px",marginBottom:14}}>
	        <div style={{fontSize:14,fontWeight:700,color:t.err,letterSpacing:.5,marginBottom:12,display:"flex",alignItems:"center",gap:8}}>⚠️ Danger Zone</div>
	        <div style={{fontSize:11,color:t.dim,lineHeight:1.55,marginBottom:14}}>Each action opens a warning before anything is deleted. The strongest actions require typed confirmation.</div>
	        <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(210px,1fr))",gap:10}}>
	          {[
	            ["🗑️","Clear All Chats","Current user's conversations",clearAllChats,false],
	            ["🧠","Clear All Memories","Current user's global and workspace memories",clearAllMemories,false],
	            ["📦","Clear All Artifacts","Current user's artifact records and removable files",clearAllArtifacts,false],
	            ["📊","Clear Statistics","Current user's token usage and analytics rows",clearStatistics,false],
	            ["🧱","Delete All Models","Every local Ollama model",deleteAllModels,true],
	            ["👥","Delete Other Users","All profiles except the current one",deleteOtherUsers,true],
	            ["☢","Delete All / Fresh Install","Everything, including current user and local models",freshInstallReset,true],
	          ].map(([icon,label,desc,onClick,strong])=><button key={label} onClick={onClick} style={{...btnS(t.err),padding:"10px 12px",minHeight:62,justifyContent:"flex-start",alignItems:"center",gap:10,background:strong?`${t.err}16`:`${t.err}0C`,borderColor:strong?`${t.err}55`:`${t.err}34`,textAlign:"left"}}>
	            <span style={{fontSize:18,width:24,textAlign:"center",flexShrink:0}}>{icon}</span>
	            <span style={{display:"flex",flexDirection:"column",gap:2,minWidth:0}}>
	              <span style={{fontSize:12,fontWeight:900,color:t.err,whiteSpace:"nowrap",overflow:"hidden",textOverflow:"ellipsis"}}>{label}</span>
	              <span style={{fontSize:10,color:t.dim,lineHeight:1.3,fontWeight:600}}>{desc}</span>
	            </span>
	          </button>)}
	        </div>
	      </div>

      {/* TILE: Changelog */}
      <div style={{display:settingsTab==="changelog"?"block":"none",padding:0,overflow:"hidden",background:"transparent",border:"none",borderRadius:0,marginBottom:0}}>
        <div style={{maxHeight:"calc(85vh - 120px)",overflowY:"auto",padding:"6px 4px 20px",lineHeight:1.7,fontSize:13,color:t.text}}>
          {!changelogContent?<div style={{textAlign:"center",padding:40,color:t.mut,display:"flex",flexDirection:"column",alignItems:"center",gap:8}}>
            <div style={{display:"flex",gap:4}}>{[0,1,2].map(i=><div key={i} style={{width:6,height:6,borderRadius:"50%",background:t.acc,animation:`pulse 1.4s ${i*.16}s infinite`}}/>)}</div>
            Loading changelog...
          </div>:<div className="changelog-markdown"><MDWrap>{md(changelogContent,{changelog:true})}</MDWrap></div>}
        </div>
      </div>

    </div>
  </div>

    {/* ── HEALTH MONITOR PANEL (slides in from right) ── */}
    {showHealthMonitor&&<div style={{flex:"0 0 50%",overflowY:"auto",padding:"20px 24px",borderLeft:`1px solid ${t.brd}22`,background:`${t.bg}`,animation:"fadeIn .3s ease"}}>
      <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:18}}>
        <h3 style={{fontSize:16,fontWeight:700,color:t.acc,letterSpacing:1,textTransform:"uppercase",margin:0}}>Service Monitor</h3>
        <button onClick={()=>setShowHealthMonitor(false)} style={{background:"none",border:`1px solid ${t.brd}33`,borderRadius:7,color:t.dim,cursor:"pointer",padding:"4px 10px",fontSize:12}}>✕ Close</button>
      </div>
      {!healthHistory?<div style={{color:t.mut,fontSize:12}}>Loading uptime data...</div>
      :Object.keys(healthHistory.services||{}).length===0?<div style={{color:t.mut,fontSize:12}}>No health data yet. Checks run every 5 minutes — data will appear shortly.</div>
      :Object.entries(healthHistory.services).map(([svc,data])=>{
        const statusColor=data.current_status==="ok"?t.ok:data.current_status==="degraded"?"#f0a030":t.err;
        const statusLabel=data.current_status==="ok"?"Operational":data.current_status==="degraded"?"Degraded":"Down";
        const days=data.days||[];
        // Pad to 90 days with gray (no data) bars
        const padded=[];
        const now=new Date();
        for(let i=89;i>=0;i--){
          const d=new Date(now);d.setDate(d.getDate()-i);
          const ds=d.toISOString().slice(0,10);
          const found=days.find(x=>x.day===ds);
          padded.push(found||{day:ds,ok_pct:0,degraded_pct:0,error_pct:0,total_checks:0,avg_ms:0});
        }
        const isRL=svc==="searxng"&&health.searxng?.rate_limited;
        return <div key={svc} style={{background:`${t.surface}33`,border:`1px solid ${t.brd}22`,borderRadius:12,padding:"14px 16px",marginBottom:14}}>
          <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:10}}>
            <div style={{display:"flex",alignItems:"center",gap:8}}>
              <span style={{fontSize:14,fontWeight:700,color:t.text,textTransform:"capitalize"}}>{svc}</span>
              {isRL&&<span style={{padding:"3px 10px",borderRadius:6,background:"#f0a03018",border:"1px solid #f0a03044",fontSize:11,fontWeight:600,color:"#f0a030"}}>Still Operational, limited engine usage</span>}
            </div>
            <div style={{fontSize:12,fontWeight:600,color:isRL?"#f0a030":statusColor}}>{isRL?"Rate Limited":statusLabel}</div>
          </div>
          {/* Uptime bar chart */}
          <div style={{display:"flex",gap:1,height:34,alignItems:"flex-end",marginBottom:6}}>
            {padded.map((d,i)=>{
              const hasData=d.total_checks>0;
              const barColor=!hasData?`${t.brd}33`:d.error_pct>50?t.err:d.error_pct>0?"#e05050":d.degraded_pct>30?"#f0a030":d.degraded_pct>0?"#b8d040":t.ok;
              const title=hasData?`${d.day}\n${d.ok_pct}% ok, ${d.degraded_pct}% degraded, ${d.error_pct}% down\n${d.total_checks} checks, avg ${d.avg_ms}ms`:`${d.day}\nNo data`;
              return <div key={i} title={title} style={{flex:1,height:hasData?34:20,background:barColor,borderRadius:2,cursor:"pointer",opacity:hasData?1:.3,transition:"opacity .15s"}}
                onMouseEnter={e=>e.target.style.opacity="0.7"} onMouseLeave={e=>e.target.style.opacity=hasData?"1":"0.3"}/>;
            })}
          </div>
          <div style={{display:"flex",justifyContent:"space-between",alignItems:"center"}}>
            <span style={{fontSize:10,color:t.mut}}>90 days ago</span>
            <span style={{fontSize:11,color:t.dim,fontWeight:600}}>{data.uptime_pct}% uptime</span>
            <span style={{fontSize:10,color:t.mut}}>Today</span>
          </div>
          {data.avg_response_ms>0&&<div style={{fontSize:10,color:t.mut,marginTop:4}}>Avg response: {data.avg_response_ms}ms</div>}
        </div>;
      })}

      {/* Legend */}
      <div style={{display:"flex",gap:12,padding:"8px 0",fontSize:11,color:t.dim}}>
        {[[t.ok,"Operational"],["#f0a030","Degraded"],[t.err,"Down"],[`${t.brd}33`,"No data"]].map(([c,l])=><div key={l} style={{display:"flex",alignItems:"center",gap:5}}>
          <div style={{width:12,height:12,borderRadius:2,background:c}}/>{l}
        </div>)}
      </div>

      {/* Refresh button */}
      <button onClick={async()=>{try{const r=await fetch(`${API}/api/health/history?days=90`);const d=await r.json();setHealthHistory(d);}catch{}
        try{const r2=await fetch(`${API}/api/health`);const d2=await r2.json();setHealth(d2.services||{});}catch{}
      }} style={{...btnS(t.f1),fontSize:12,padding:"6px 14px",marginTop:4}}>🔄 Refresh</button>
    </div>}
  </div>
    </div>
  </div>,document.body)

      :<>
        {/* CHAT - full width, pills inline */}
        <div style={{flex:1,display:"flex",overflow:"hidden",position:"relative"}}
          onDragOver={e=>{e.preventDefault();e.stopPropagation();setDragOver(true);}}
          onDragLeave={e=>{e.preventDefault();e.stopPropagation();setDragOver(false);}}
          onDrop={e=>{e.preventDefault();e.stopPropagation();setDragOver(false);const files=Array.from(e.dataTransfer.files);if(files.length)handleFileUpload(files);}}>
          {/* Drag overlay */}
          {dragOver&&<div style={{position:"absolute",inset:0,zIndex:50,background:`${t.acc}12`,border:`3px dashed ${t.acc}`,borderRadius:16,display:"flex",alignItems:"center",justifyContent:"center",backdropFilter:"blur(4px)",pointerEvents:"none"}}>
            <div style={{fontSize:15,color:t.acc,fontWeight:700,letterSpacing:1}}>{isCoderPersonaName(act?.persona_name)?"Drop files or a project archive (.zip, .tar.gz)":"Drop files to attach (PDFs supported)"}</div>
          </div>}
          {/* Scroll-to-top / Scroll-to-bottom */}
          {showScrollTop&&<button onClick={()=>chatScrollRef.current?.scrollTo({top:0,behavior:"smooth"})} style={{position:"absolute",top:12,right:20,zIndex:20,...glass,border:`1px solid ${t.brd}33`,borderRadius:"50%",width:32,height:32,display:"flex",alignItems:"center",justifyContent:"center",cursor:"pointer",color:t.mut,fontSize:14,boxShadow:`0 2px 8px ${t.bg}88`}} title="Scroll to top">{"\u2191"}</button>}
          {showScrollBottom&&<button onClick={()=>chatScrollRef.current?.scrollTo({top:chatScrollRef.current.scrollHeight,behavior:"smooth"})} style={{position:"absolute",bottom:12,right:20,zIndex:20,...glass,border:`1px solid ${t.brd}33`,borderRadius:"50%",width:32,height:32,display:"flex",alignItems:"center",justifyContent:"center",cursor:"pointer",color:t.mut,fontSize:14,boxShadow:`0 2px 8px ${t.bg}88`}} title="Scroll to bottom">{"\u2193"}</button>}
          {/* Messages */}
          <div ref={chatScrollRef} onScroll={e=>{const el=e.target;setShowScrollTop(el.scrollTop>400);setShowScrollBottom(el.scrollHeight-el.scrollTop-el.clientHeight>400);}} style={{flex:1,overflowY:"auto",padding:"20px 0 18px"}}>
          {!act||!(act.messages||[]).length&&!councilRunning?<div style={{display:"flex",flexDirection:"column",alignItems:"center",justifyContent:"center",height:"100%",gap:10,opacity:(loadingConv||act?.is_council)?0.68:1,paddingBottom:isEmptyChatSurface?250:0,pointerEvents:"none",transition:"padding-bottom .35s ease"}}>
            {loadingConv?<><div style={{display:"flex",gap:4}}>{[0,1,2].map(i=><div key={i} style={{width:6,height:6,borderRadius:"50%",background:t.acc,animation:`pulse 1.4s ${i*.16}s infinite`}}/>)}</div><div style={{fontSize:12,color:t.mut,letterSpacing:1}}>Loading conversation...</div></>
            :act?.is_council?<><div style={{fontSize:36,animation:"float 4s ease-in-out infinite"}}>⚖️</div><div style={{fontSize:12,color:t.mut,letterSpacing:1}}>{loadingConv?"Loading council history...":"Ask the council"}</div></>
            :<div style={{display:"flex",flexDirection:"column",alignItems:"center",gap:16,animation:"fadeIn .35s both",transform:isEmptyChatSurface?"translateY(-34px)":"none"}}>
              <div style={{width:68,height:68,borderRadius:18,background:t.bgDeep,border:`1px solid ${t.brd}77`,position:"relative",overflow:"hidden",boxShadow:`0 0 26px ${t.acc}14`,display:"flex",alignItems:"center",justifyContent:"center"}}>
                <div style={{position:"absolute",inset:6,borderRadius:13,border:`1px solid ${t.brd}33`,background:`${t.surface}55`}}/>
                <div style={{position:"absolute",left:23,top:18,width:10,height:32,borderRadius:2,background:t.acc,opacity:.95}}/>
                <div style={{position:"absolute",right:23,bottom:18,width:10,height:32,borderRadius:2,background:t.warm,opacity:.9}}/>
              </div>
              <div style={{display:"flex",flexDirection:"column",alignItems:"center",gap:8}}>
                <div style={{fontSize:32,fontWeight:800,color:t.acc,letterSpacing:.7,lineHeight:1}}>HyprChat</div>
                <div style={{width:120,height:1,background:`linear-gradient(90deg,transparent,${t.acc}88,transparent)`}}/>
                <div style={{fontSize:13,color:t.dim,letterSpacing:1.8,textAlign:"center",maxWidth:520,lineHeight:1.7,textTransform:"none"}}>{dailyWelcome}</div>
              </div>
            </div>}
          </div>:act?.is_council?(()=>{
            const councilCfg=councils.find(c=>c.id===act.council_config_id);
            const getMeta=m=>{if(typeof m.metadata==="string"){try{return JSON.parse(m.metadata);}catch{return{};}}return m.metadata||{};};
            const members=councilCfg?.members||[];
            const turns=[];let curTurn=null;
            for(const msg of(act.messages||[])){
              const meta=getMeta(msg);
              if(msg.role==="user"&&!meta.council_member_id&&!meta.council_host){
                curTurn={user:msg,rounds:{},host:null};turns.push(curTurn);
              }else if(msg.role==="assistant"){
                if(!curTurn){curTurn={user:{role:"user",content:""},rounds:{},host:null};turns.push(curTurn);}
                if(meta.council_host)curTurn.host=msg;
                else{
                  const rnd=meta.debate_round||0;
                  if(!curTurn.rounds[rnd])curTurn.rounds[rnd]=[];
                  curTurn.rounds[rnd].push(msg);
                }
              }
            }
            const isLastTurn=ti=>ti===turns.length-1;
            const memberProfile=m=>m?.model_config_id?mcs.find(mc=>mc.id===m.model_config_id):null;
            const memberById=id=>members.find(m=>m.id===id)||{};
            const memberView=(id,meta={},live={})=>{
              const member=memberById(id);
              const mc=memberProfile(member);
              const model=meta.council_model||live.model||mc?.base_model||member.model||"";
              const name=meta.council_persona||live.member_name||mc?.name||member.persona_name||model.split(":")[0]||"Council member";
              const avatar=mc?profileAvatar(mc):"";
              const pts=member.points||0;
              return {member,mc,model,name,avatar,pts};
            };
            const chip=(color,children,extra={})=><span style={{display:"inline-flex",alignItems:"center",gap:4,padding:"3px 7px",borderRadius:7,background:`${color}12`,border:`1px solid ${color}30`,color,fontSize:10,fontWeight:800,whiteSpace:"nowrap",...extra}}>{children}</span>;
            const avatarNode=(view,size=34,color=t.acc)=><div style={{width:size,height:size,borderRadius:9,display:"flex",alignItems:"center",justifyContent:"center",background:`${color}10`,border:`1px solid ${color}38`,color,overflow:"hidden",flexShrink:0}}>
              {view.avatar?<img src={avatarSrc(view.avatar)} style={{width:"100%",height:"100%",objectFit:"cover"}} alt=""/>:<IC.Bot/>}
            </div>;
            const tallyFromVotes=votes=>votes.reduce((a,v)=>({...a,[v.voted_for]:(a[v.voted_for]||0)+1}),{});
            const sortedTally=tally=>Object.entries(tally).sort((a,b)=>b[1]-a[1]).map(([id,votes])=>({id,votes,view:memberView(id)}));
            const livePhase=councilRunning?(councilVoting?"Peer vote":councilRound?.label||"Deliberating"):"Session history";
            const responseCard=(resp,ri,roundKey,tally,live=false)=>{
              const meta=live?resp.metadata:getMeta(resp);
              const memberId=meta.council_member_id;
              const view=memberView(memberId,meta,live?resp.live:{});
              const votes=tally[memberId]||0;
              const ptsColor=view.pts>0?t.ok:view.pts<0?t.err:t.mut;
              const ckey=`council-card-${roundKey}-${ri}`;
              const expanded=expandedRounds[ckey]===true;
              const respondingTo=Array.isArray(meta.responding_to)?meta.responding_to:[];
              const activeBorder=votes>0?t.ok:(roundKey===0?t.acc:t.pink);
              return <div key={ckey} style={{background:`linear-gradient(180deg,${t.surface}E8,${t.bgDeep}D8)`,border:`1px solid ${activeBorder}${votes>0?"66":"30"}`,borderRadius:10,overflow:"hidden",boxShadow:votes>0?`0 0 0 1px ${activeBorder}18, 0 10px 30px #0002`:"0 10px 24px #0001",minWidth:0}}>
                <div style={{padding:11,borderBottom:`1px solid ${t.brd}22`,display:"flex",gap:9,alignItems:"center"}}>
                  {avatarNode(view,34,roundKey===0?t.acc:t.pink)}
                  <div style={{minWidth:0,flex:1}}>
                    <div style={{display:"flex",alignItems:"center",gap:6,minWidth:0}}>
                      <span style={{fontSize:12,fontWeight:900,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{view.name}</span>
                      {view.mc&&chip(t.pink,"Persona",{fontSize:9,padding:"2px 6px"})}
                    </div>
                    <div style={{fontSize:9,color:t.mut,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",marginTop:2}}>{view.model||"model unavailable"}</div>
                  </div>
                  <div style={{display:"flex",gap:5,alignItems:"center",flexShrink:0}}>
                    {votes>0&&chip(t.ok,<><IC.Trophy/>{votes}</>,{fontSize:9})}
                    {chip(ptsColor,`${view.pts>0?"+":""}${view.pts} pt`,{fontSize:9})}
                  </div>
                </div>
                {respondingTo.length>0&&<div style={{padding:"8px 11px 0",display:"flex",alignItems:"center",gap:6,flexWrap:"wrap"}}>
                  <span style={{fontSize:9,textTransform:"uppercase",letterSpacing:.7,color:t.mut,fontWeight:900}}>Responding to</span>
                  {respondingTo.slice(0,4).map(r=>chip(t.acc,r.name||r.id,{fontSize:9,padding:"2px 6px"}))}
                </div>}
                <div ref={el=>{if(live&&!resp.live.done&&el)el.scrollTop=el.scrollHeight;}} style={{padding:"10px 12px",fontSize:fontSize-1,lineHeight:1.7,color:t.dim,maxHeight:expanded?760:300,overflowY:"auto"}}>
                  <MDWrap>{md(resp.content||"")}</MDWrap>
                  {live&&!resp.live.done&&<span style={{display:"inline-block",width:2,height:13,background:t.acc,marginLeft:2,animation:"blink .8s step-end infinite",verticalAlign:"text-bottom"}}/>}
                </div>
                <div style={{padding:"7px 10px",borderTop:`1px solid ${t.brd}18`,display:"flex",gap:6,alignItems:"center"}}>
                  <button onClick={()=>setExpandedRounds(p=>({...p,[ckey]:!expanded}))} style={{...btnS(t.mut),padding:"5px 7px",fontSize:9}}>{expanded?"Compact":"Expand"}</button>
                  {memberId&&<>
                    <button onClick={()=>scoreCouncilMember(act.council_config_id,memberId,5)} title="Mark best answer (+5)" style={{...btnS(t.ok),padding:"5px 7px",fontSize:9}}><IC.Star/> Best</button>
                    <button onClick={()=>scoreCouncilMember(act.council_config_id,memberId,2)} title="Add points (+2)" style={{...btnS(t.acc),padding:"5px 7px",fontSize:9}}><IC.ChevDown style={{transform:"rotate(180deg)"}}/> Good</button>
                    <button onClick={()=>scoreCouncilMember(act.council_config_id,memberId,-2)} title="Subtract points (-2)" style={{...btnS(t.err),padding:"5px 7px",fontSize:9}}><IC.ChevDown/> Weak</button>
                  </>}
                  <button onClick={()=>cp(resp.content,ckey)} style={{...btnS(t.mut),padding:"5px 7px",fontSize:9,marginLeft:"auto"}}>{copied===ckey?<IC.Check/>:<IC.Copy/>}</button>
                </div>
              </div>;
            };
            const ballotPanel=(votes,hostMeta={})=>{
              if(!votes.length&&!councilVoting)return null;
              const tally=tallyFromVotes(votes);
              const leaders=hostMeta.winners||sortedTally(tally).map(x=>({id:x.id,name:x.view.name,votes:x.votes}));
              return <div style={{background:`${t.surface}92`,border:`1px solid ${t.pink}32`,borderRadius:10,padding:12,margin:"12px 0",display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(260px,1fr))",gap:12}}>
                <div>
                  <div style={{display:"flex",alignItems:"center",gap:7,marginBottom:8,color:t.pink,fontSize:11,fontWeight:900,textTransform:"uppercase",letterSpacing:.7}}><IC.Trophy/> Peer Ballot</div>
                  {leaders.length?<div style={{display:"flex",flexDirection:"column",gap:6}}>
                    {leaders.map((l,i)=><div key={l.id||i} style={{display:"flex",alignItems:"center",gap:8,padding:"7px 8px",borderRadius:8,background:i===0?`${t.ok}12`:`${t.bgDeep}72`,border:`1px solid ${i===0?t.ok:t.brd}30`}}>
                      <span style={{fontSize:10,color:i===0?t.ok:t.mut,fontWeight:900,width:18}}>{i+1}</span>
                      <span style={{fontSize:11,color:t.text,fontWeight:800,flex:1,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{l.name}</span>
                      {chip(i===0?t.ok:t.mut,`${l.votes} vote${l.votes===1?"":"s"}`,{fontSize:9})}
                    </div>)}
                  </div>:<div style={{fontSize:11,color:t.mut,lineHeight:1.5}}>Waiting for council members to cast their peer votes.</div>}
                </div>
                <div style={{display:"flex",flexDirection:"column",gap:7}}>
                  {votes.length?votes.map((v,vi)=><div key={vi} style={{display:"flex",flexWrap:"wrap",gap:8,alignItems:"center",padding:"7px 8px",borderRadius:8,background:`${t.bgDeep}72`,border:`1px solid ${t.brd}22`}}>
                    <span style={{fontSize:11,color:t.acc,fontWeight:800,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",minWidth:90,maxWidth:150}}>{v.voter_name}</span>
                    <span style={{height:1,background:`linear-gradient(90deg,${t.acc}55,${t.ok}55)`,width:28,flexShrink:0}}/>
                    <span style={{fontSize:11,color:t.ok,fontWeight:900,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",minWidth:100,maxWidth:170}}>{v.voted_for_name}</span>
                    <span style={{fontSize:10,color:t.mut,lineHeight:1.45,flex:"1 1 220px",minWidth:0}}>{v.reason||"No reason supplied."}</span>
                  </div>):<div style={{display:"flex",alignItems:"center",gap:5,color:t.pink,fontSize:11,fontWeight:800}}>
                    {[0,1,2].map(i=><span key={i} style={{width:5,height:5,borderRadius:"50%",background:t.pink,animation:`pulse 1.4s ${i*.16}s infinite`}}/>)} Voting in progress
                  </div>}
                </div>
              </div>;
            };
            return <div key={actId||"empty"} style={{maxWidth:1540,margin:"0 auto",padding:"0 20px 8px",overflowY:"auto"}}>
              <div style={{position:"sticky",top:0,zIndex:8,margin:"0 0 16px",padding:"10px 0 8px",background:`linear-gradient(180deg,${t.bg}F8,${t.bg}D8 70%,transparent)`,backdropFilter:"blur(8px)"}}>
                <div style={{display:"flex",alignItems:"center",gap:10,background:`${t.surface}B8`,border:`1px solid ${t.brd}30`,borderRadius:10,padding:"10px 12px"}}>
                  <div style={{width:34,height:34,borderRadius:9,display:"flex",alignItems:"center",justifyContent:"center",background:`${t.pink}12`,border:`1px solid ${t.pink}35`,color:t.pink}}><IC.Council/></div>
                  <div style={{minWidth:0,flex:1}}>
                    <div style={{display:"flex",gap:8,alignItems:"center",flexWrap:"wrap"}}>
                      <span style={{fontSize:13,fontWeight:900,color:t.text,letterSpacing:.4}}>{councilCfg?.name||"Council Session"}</span>
                      {chip(t.pink,`${members.length} members`)}
                      {chip(councilRunning?t.ok:t.mut,livePhase)}
                      {councilRound?.total_rounds>1&&chip(t.acc,`Round ${(councilRound.round||0)+1}/${councilRound.total_rounds}`)}
                    </div>
                    <div style={{fontSize:10,color:t.mut,marginTop:3,whiteSpace:"nowrap",overflow:"hidden",textOverflow:"ellipsis"}}>{members.map(m=>memberView(m.id).name).join(" / ")||"No council members configured"}</div>
                  </div>
                  {councilCfg?.host_model&&chip(t.warm,<><IC.Crown/>{councilCfg.host_model}</>)}
                </div>
              </div>
              {turns.map((turn,ti)=>{
                const hostMeta=turn.host?getMeta(turn.host):{};
                const isLive=councilRunning&&isLastTurn(ti);
                const turnVotes=isLive?councilVotes:(hostMeta.votes||[]);
                const turnTally=tallyFromVotes(turnVotes);
                const roundNums=Object.keys(turn.rounds).map(Number).sort((a,b)=>a-b);
                return <div key={ti} style={{display:"grid",gridTemplateColumns:"26px minmax(0,1fr)",gap:12,marginBottom:28,animation:"fadeIn .3s both"}}>
                  <div style={{display:"flex",flexDirection:"column",alignItems:"center",gap:6}}>
                    <div style={{width:26,height:26,borderRadius:8,display:"flex",alignItems:"center",justifyContent:"center",background:`${t.f4}10`,border:`1px solid ${t.f4}35`,color:t.f4}}><IC.User/></div>
                    <div style={{width:1,flex:1,minHeight:30,background:`linear-gradient(${t.f4}45,${t.pink}33,${t.warm}35)`}}/>
                  </div>
                  <div>
                    <div style={{background:`${t.surface}D8`,border:`1px solid ${t.brd}38`,borderRadius:10,padding:"12px 14px",marginBottom:14}}>
                      <div style={{fontSize:10,color:t.f4,fontWeight:900,textTransform:"uppercase",letterSpacing:.8,marginBottom:6}}>Address to the council</div>
                      <div style={{fontSize:fontSize,lineHeight:1.65,color:t.text}}><MDWrap>{md(turn.user.content)}</MDWrap></div>
                    </div>
                    {roundNums.map(rnd=>{
                      const rKey=`${ti}-${rnd}`;
                      const resps=turn.rounds[rnd]||[];
                      const label=getMeta(resps[0]||{}).round_label||(rnd===0?"Opening Statements":`Rebuttal Round ${rnd}`);
                      const isExpanded=expandedRounds[rKey]!==undefined?expandedRounds[rKey]:true;
                      return <div key={rnd} style={{marginBottom:14}}>
                        <button onClick={()=>setExpandedRounds(p=>({...p,[rKey]:!isExpanded}))} style={{width:"100%",border:`1px solid ${rnd===0?t.acc:t.pink}28`,background:`${rnd===0?t.acc:t.pink}08`,borderRadius:9,padding:"8px 10px",display:"flex",alignItems:"center",gap:8,cursor:"pointer",color:rnd===0?t.acc:t.pink,fontFamily:font}}>
                          <span style={{transform:isExpanded?"rotate(0deg)":"rotate(-90deg)",transition:"transform .15s"}}><IC.ChevDown/></span>
                          {rnd===0?<IC.Council/>:<IC.GitBranch/>}
                          <span style={{fontSize:11,fontWeight:900,textTransform:"uppercase",letterSpacing:.8}}>{label}</span>
                          <span style={{fontSize:10,color:t.mut,marginLeft:"auto"}}>{resps.length} response{resps.length===1?"":"s"}</span>
                        </button>
                        {isExpanded&&<div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(280px,1fr))",gap:12,marginTop:10}}>
                          {resps.map((resp,ri)=>responseCard(resp,ri,rnd,turnTally,false))}
                        </div>}
                      </div>;
                    })}
                    {isLive&&councilRound&&<div style={{marginBottom:14}}>
                      <div style={{border:`1px solid ${t.pink}28`,background:`${t.pink}08`,borderRadius:9,padding:"8px 10px",display:"flex",alignItems:"center",gap:8,color:t.pink}}>
                        <IC.GitBranch/><span style={{fontSize:11,fontWeight:900,textTransform:"uppercase",letterSpacing:.8}}>{councilRound.label}</span>
                        <div style={{display:"flex",gap:4,marginLeft:"auto"}}>{[0,1,2].map(i=><span key={i} style={{width:5,height:5,borderRadius:"50%",background:t.pink,animation:`pulse 1.4s ${i*.16}s infinite`}}/>)}</div>
                      </div>
                      {councilKbStatus&&<div style={{fontSize:10,color:t.dim,margin:"7px 2px 0"}}>{councilKbStatus}</div>}
                      <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(280px,1fr))",gap:12,marginTop:10}}>
                        {Object.entries(councilResponses).map(([mid,resp],ri)=>responseCard({content:resp.content||"",metadata:{council_member_id:mid,council_model:resp.model,council_persona:resp.member_name,debate_round:resp.round,round_label:resp.round_label,responding_to:resp.responding_to||[]},live:resp},ri,`live-${resp.round||0}`,turnTally,true))}
                      </div>
                    </div>}
                    {ballotPanel(turnVotes,hostMeta)}
                    {(turn.host||(isLive&&councilHostContent))&&<div style={{background:`linear-gradient(180deg,${t.warm}10,${t.surface}C8)`,border:`1px solid ${t.warm}34`,borderRadius:10,padding:14,marginTop:12,animation:"fadeIn .3s"}}>
                      <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:10}}>
                        <div style={{width:28,height:28,borderRadius:8,display:"flex",alignItems:"center",justifyContent:"center",background:`${t.warm}12`,border:`1px solid ${t.warm}34`,color:t.warm}}><IC.Crown/></div>
                        <div style={{minWidth:0,flex:1}}>
                          <div style={{fontSize:12,fontWeight:900,color:t.warm,textTransform:"uppercase",letterSpacing:.8}}>Moderator Verdict</div>
                          <div style={{fontSize:10,color:t.mut}}>{councilCfg?.host_model||act.model||"host model"}</div>
                        </div>
                        {(hostMeta.winner||hostMeta.winners?.[0])&&chip(t.ok,<><IC.Trophy/>{(hostMeta.winner||hostMeta.winners[0]).name}</>)}
                        {isLive&&!turn.host&&<div style={{display:"flex",gap:4}}>{[0,1,2].map(i=><span key={i} style={{width:5,height:5,borderRadius:"50%",background:t.warm,animation:`pulse 1.4s ${i*.16}s infinite`}}/>)}</div>}
                      </div>
                      <div ref={el=>{if(el&&isLive&&!turn.host)el.scrollTop=el.scrollHeight;}} style={{fontSize:fontSize-1,lineHeight:1.75,color:t.dim,maxHeight:520,overflowY:"auto"}}>
                        <MDWrap>{md(turn.host?.content||councilHostContent)}</MDWrap>
                        {isLive&&councilHostContent&&!turn.host&&<span style={{display:"inline-block",width:2,height:13,background:t.warm,marginLeft:2,animation:"blink .8s step-end infinite",verticalAlign:"text-bottom"}}/>}
                      </div>
                    </div>}
                    {quickResults.length>0&&isLastTurn(ti)&&<QuickSearchSourcesPanel payload={_quickSearchPayloadFromEvents(evts,quickResults)} t={t} font={font}/>}
                  </div>
                </div>;
              })}
              {councilRunning&&!turns.length&&Object.keys(councilResponses).length>0&&<div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(280px,1fr))",gap:12}}>
                {Object.entries(councilResponses).map(([mid,resp],ri)=>responseCard({content:resp.content||"",metadata:{council_member_id:mid,council_model:resp.model,council_persona:resp.member_name,debate_round:resp.round,round_label:resp.round_label,responding_to:resp.responding_to||[]},live:resp},ri,`initial-${resp.round||0}`,{},true))}
              </div>}
              <div ref={chatEnd}/>
            </div>;
          })():<div key={actId||"empty"} style={{maxWidth:chatWidth,margin:"0 auto",padding:"2px 18px 0"}}>
            {(()=>{const messageRows=(act.messages||[]).map((msg,index)=>({msg,index})).filter(({msg})=>msg.role==="user"||msg.role==="assistant");
              const totalMessages=messageRows.length;
              const visibleCount=Math.max(CHAT_HISTORY_RENDER_CHUNK,visibleMessageCounts[actId]||CHAT_HISTORY_RENDER_CHUNK);
              const hiddenCount=Math.max(0,totalMessages-visibleCount);
              const visibleRows=hiddenCount>0?messageRows.slice(hiddenCount):messageRows;
              // Find the last assistant message once so live search/tool events attach to the active answer.
              let lastAssistantMsgIdx=-1;
              messageRows.forEach(({msg,index})=>{if(msg.role==="assistant")lastAssistantMsgIdx=index;});
              return <>
              {hiddenCount>0&&<div style={{display:"flex",justifyContent:"center",margin:"0 0 14px"}}>
                <button onClick={()=>revealOlderMessages(actId,totalMessages)} style={{display:"inline-flex",alignItems:"center",gap:6,padding:"6px 12px",borderRadius:8,border:`1px solid ${t.brd}44`,background:`${t.surface}B8`,color:t.mut,cursor:"pointer",fontFamily:font,fontSize:11,fontWeight:700}}>
                  <span style={{display:"inline-flex",transform:"rotate(180deg)"}}><IC.ChevDown/></span>
                  Show older ({hiddenCount})
                </button>
              </div>}
              {visibleRows.map(({msg,index:i},visibleIdx)=>{const isU=msg.role==="user";const mid=`m${i}`;const isEditing=editingMsg?.index===i;
              const activeProfile=!isU?getProfileForConversation(act):null;
              const personaAvatar=!isU&&(act?.persona_avatar||profileAvatar(activeProfile));
              const personaName=!isU&&act?.persona_name;
              const msgProfileType=personaName?getConversationProfileType(act):null;
              const personaColor=!isU&&personaName?(msgProfileType==="persona"?t.pink:t.acc):t.acc;
              const renderedContent=!isU?replacePersonaPlaceholdersForConversation(msg.content||"",act):msg.content;
              const meta=_metaObj(msg.metadata);
              const savedEvents=Array.isArray(meta.saved_events)?meta.saved_events:[];
              const isLastAssistant=!isU&&i===lastAssistantMsgIdx;
              const liveEventsForMsg=isLastAssistant?evts:[];
              const messageWorkflows=isLastAssistant&&Array.isArray(coderWorkflows)?coderWorkflows.slice(0,3):[];
              const daedalusRunIds=!isU?_daedalusRunIdsForMessage(meta,isLastAssistant,evts):[];
              const isDaedalusFullBuild=!isU&&_isDaedalusFullBuildOutput({meta,savedEvents,liveEvents:liveEventsForMsg,runIds:daedalusRunIds,workflows:messageWorkflows});
              const isDaedalusOutput=!isU&&_isDaedalusOutput({meta,savedEvents,liveEvents:liveEventsForMsg,runIds:daedalusRunIds,workflows:messageWorkflows});
              const liveQuickSearchPayload=isLastAssistant?_quickSearchPayloadFromEvents(evts,quickResults):null;
              const renderOpts=citeOptsFor(msg,liveQuickSearchPayload);
              const quickSearchPayload=renderOpts?.quickSearch;
              return <React.Fragment key={msg.id?`${msg.id}-${i}`:i}>
              <div style={{marginBottom:compactMode?7:18,display:"flex",gap:9,alignItems:"flex-start",animation:`fadeIn .3s ${Math.min(visibleIdx*.04,.2)}s both`}}>
                <div style={{width:isU?28:40,height:isU?28:40,borderRadius:isU?7:10,display:"flex",alignItems:"center",justifyContent:"center",background:isU?t.bgDeep:`${personaColor}10`,border:`1px solid ${isU?t.f4:personaColor}38`,color:isU?t.f4:personaColor,flexShrink:0,marginTop:isU?3:1,overflow:"hidden",boxShadow:"none"}}>
                  {isU?<IC.User/>:personaAvatar?<img src={avatarSrc(personaAvatar)} style={{width:"100%",height:"100%",objectFit:"cover"}} alt=""/>:<IC.Bot/>}
                </div>
                <div style={{flex:1,minWidth:0,maxWidth:isU?"min(760px,92%)":"100%"}}>
                  <div style={{fontSize:11,marginBottom:5,fontWeight:600,letterSpacing:.4,display:"flex",alignItems:"center",gap:6}}>
                    {isU?<span style={{color:t.mut}}>you</span>:personaName
                      ?<span style={{display:"inline-flex",alignItems:"center",gap:4,padding:"1px 7px",background:`${t.pink}15`,border:`1px solid ${t.pink}28`,borderRadius:10,color:t.pink}}>
                        {personaAvatar&&<img src={avatarSrc(personaAvatar)} style={{width:14,height:14,borderRadius:4,objectFit:"cover"}} alt=""/>}
                        {personaName}
                      </span>
                      :<span style={{color:t.mut}}>{act.model||"assistant"}</span>}
                    {msg.created_at&&<span style={{fontSize:9,color:t.mut,opacity:.4,marginLeft:2}}>{new Date(msg.created_at).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"})}</span>}
                    {!isU&&!msg.isS&&(()=>{const meta=typeof msg.metadata==="string"?(()=>{try{return JSON.parse(msg.metadata);}catch{return{};}})():(msg.metadata||{});const n=meta.refinements||0;if(n<=0)return null;const lvl=EFFORT_LEVELS[n]||EFFORT_LEVELS[EFFORT_LEVELS.length-1];return <span title={`Refined ${n}× via ${lvl.name}`} style={{fontSize:9,padding:"1px 6px",borderRadius:8,background:`${t.pink}15`,border:`1px solid ${t.pink}30`,color:t.pink,display:"inline-flex",alignItems:"center",gap:3,fontWeight:600}}>✨ Refined {n}×</span>;})()}
                  </div>
                  <div style={{background:isU?`${t.surface}DE`:"transparent",padding:isU?"9px 13px":"0",borderRadius:isU?8:0,border:isU?`1px solid ${t.brd}34`:"none",lineHeight:1.68,fontSize:fontSize,color:isU?t.text:t.dim}}>
                    {isEditing?<div style={{display:"flex",flexDirection:"column",gap:6}}>
                      <textarea defaultValue={msg.content} ref={el=>{if(el&&!el._set){el._set=true;el.style.height=Math.min(el.scrollHeight,isU?200:420)+"px";}}} style={{width:"100%",background:t.bgDeep,border:`1px solid ${t.acc}44`,color:t.text,padding:"8px 10px",borderRadius:6,fontFamily:font,fontSize:12,outline:"none",resize:"vertical",lineHeight:1.5,boxSizing:"border-box"}}
                        onChange={e=>setEditingMsg(p=>({...p,content:e.target.value}))}/>
                      <div style={{display:"flex",gap:4}}>
                        {isU
                          ?<button onClick={()=>editMessage(i,editingMsg.content)} style={btnS(t.ok)}>Save & Send</button>
                          :<button onClick={()=>saveAssistantEdit(i,editingMsg.content)} style={btnS(t.ok)}>Save</button>}
                        <button onClick={()=>setEditingMsg(null)} style={btnS(t.mut)}>Cancel</button>
                      </div>
                    </div>
                    :isDaedalusOutput?null:msg.isS?mdStream(renderedContent,renderOpts):<MemoMD content={renderedContent} md={md} opts={renderOpts}/>}
                    {isU&&msg.metadata?.images?.length>0&&!isEditing&&<div style={{display:"flex",flexWrap:"wrap",gap:8,marginTop:msg.content?10:0}}>
                      {msg.metadata.images.map((img,ii)=><div key={ii} title={img.name} style={{display:"inline-flex",flexDirection:"column",gap:4,alignItems:"flex-start",maxWidth:260}}>
                        <img src={img.dataUrl} alt={img.name} onClick={()=>openPreview&&openPreview(img.name,img.dataUrl)} style={{maxWidth:260,maxHeight:200,borderRadius:8,border:`1px solid ${t.acc}33`,cursor:"pointer",display:"block",boxShadow:"none"}}/>
                        <span style={{fontSize:9,color:t.mut,fontWeight:500,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",maxWidth:260}}>📷 {img.name}</span>
                      </div>)}
                    </div>}
                    {isU&&msg.metadata?.pdfs?.length>0&&!isEditing&&<div style={{display:"flex",flexWrap:"wrap",gap:6,marginTop:msg.content?8:0}}>
                      {msg.metadata.pdfs.map((p,pi)=><span key={pi} style={{display:"inline-flex",alignItems:"center",gap:6,background:`${t.err}10`,border:`1px solid ${t.err}25`,padding:"5px 12px",borderRadius:8,fontSize:11,fontWeight:500,color:t.text}}>
                        <span style={{fontSize:16}}>📄</span>
                        <span>{p.name}</span>
                        {p.pages>0&&<span style={{fontSize:9,color:t.mut,fontWeight:400}}>{p.pages} pages</span>}
                      </span>)}
                    </div>}
                    {msg.isS&&msg.content&&!isEditing&&<span style={{display:"inline-block",width:2,height:14,background:t.acc,marginLeft:1,animation:"blink .8s step-end infinite",verticalAlign:"text-bottom"}}/>}
                    {msg.isS&&!msg.content&&!isDaedalusOutput&&(()=>{const isLast=isLastAssistant;return isLast&&evts.length>0?<ToolStatus evts={evts} savedEvts={msg.metadata?.saved_events||[]} msgContent={msg.content} t={t} expandedPill={expandedPill} setExpandedPill={setExpandedPill} onPreview={openPreview} onOpenArtifact={openArtifact} md={md}/>:<div style={{display:"flex",gap:4,padding:"6px 0"}}>{[0,1,2].map(i=><div key={i} style={{width:5,height:5,borderRadius:"50%",background:t.acc,animation:`pulse 1.4s ${i*.16}s infinite`}}/>)}</div>;})()}
                  </div>
                  {isDaedalusOutput&&<DaedalusSummary runIds={daedalusRunIds} savedEvents={savedEvents} liveEvts={liveEventsForMsg} workflows={messageWorkflows} t={t} font={font} md={md} msgContent={renderedContent} live={!!msg.isS} onPreview={openPreview} onOpenArtifact={openArtifact}/>}
                  {msg.isS&&msg.content&&!isDaedalusOutput&&(()=>{const isLast=isLastAssistant;return isLast&&evts.length>0?<ToolStatus evts={evts} savedEvts={msg.metadata?.saved_events||[]} msgContent={msg.content} t={t} expandedPill={expandedPill} setExpandedPill={setExpandedPill} onPreview={openPreview} onOpenArtifact={openArtifact} md={md}/>:null;})()}
                  {!msg.isS&&!isDaedalusOutput&&(()=>{const isLast=isLastAssistant;const filteredEvts=evts.filter(e=>(e.data?.tool||"")!=="processing");return isLast&&filteredEvts.length>0?<ToolStatus evts={filteredEvts} savedEvts={msg.metadata?.saved_events||[]} msgContent={msg.content} historical={true} t={t} expandedPill={expandedPill} setExpandedPill={setExpandedPill} onPreview={openPreview} onOpenArtifact={openArtifact} md={md}/>:null;})()}
                  {(()=>{if(isU||isDaedalusOutput||msg.isS||!savedEvents.length)return null;const isLast=isLastAssistant;if(isLast&&evts.length>0)return null;return <ToolStatus evts={savedEvents.filter(e=>(e.data?.tool||"")!=="processing")} historical={true} msgContent={msg.content} t={t} expandedPill={expandedPill} setExpandedPill={setExpandedPill} onPreview={openPreview} onOpenArtifact={openArtifact} md={md}/>;})()}
                  {(()=>{if(isU||isDaedalusOutput)return null;if(!isLastAssistant||!coderWorkflows.length)return null;return coderWorkflows.slice(0,3).map(w=><WorkflowCard key={w.id} workflow={w} t={t} font={font} onOpenArtifact={openArtifact}/>);})()}
                  {/* Coder Bot v2 — durable run cards. Render one card per unique run_id.
                      Sources, in priority: explicit metadata.run_ids (written server-side at
                      each round boundary — survives mid-stream reload), then live events
                      (current message), then saved_events (history). */}
                  {(()=>{
                    if(isU||isDaedalusOutput) return null;
                    const isLast = isLastAssistant;
                    // Merge run_ids from all three sources, dedup, preserve first-seen order.
                    const ridsFromMeta = Array.isArray(msg.metadata?.run_ids) ? msg.metadata.run_ids : [];
                    const ridsFromEvts = _runIdsFromEvents(isLast && evts.length>0 ? evts : (msg.metadata?.saved_events||[]));
                    const seen = new Set();
                    const runIds = [];
                    for(const r of [...ridsFromMeta, ...ridsFromEvts]){
                      if(r && !seen.has(r)){seen.add(r); runIds.push(r);}
                    }
                    if(!runIds.length) return null;
                    return runIds.map(rid =>
                      <RunCard key={rid} runId={rid} liveEvts={isLast?evts:[]} t={t} font={font} md={md} onOpenArtifact={openArtifact}/>
                    );
                  })()}
                  {/* Inline code execution output */}
                  {(()=>{if(isU||isDaedalusOutput)return null;const savedEvts=savedEvents;const codeOuts=savedEvts.filter(e=>e.type==="code_output");if(!codeOuts.length)return null;return codeOuts.map((e,ci)=>{const d=e.data||{};const ckey=`${i}-${ci}`;const collapsed=collapsedOutputs.has(ckey);const toggleCollapsed=()=>setCollapsedOutputs(s=>{const n=new Set(s);if(n.has(ckey))n.delete(ckey);else n.add(ckey);return n;});return <div key={ci} style={{margin:"8px 0",borderRadius:10,overflow:"hidden",border:`1px solid ${d.success?`${t.ok}33`:`${t.err}33`}`,background:t.bgDeep}}>
                    <div onClick={toggleCollapsed} title={collapsed?"Show output":"Hide output"} style={{display:"flex",alignItems:"center",gap:6,padding:"6px 12px",background:`${d.success?t.ok:t.err}10`,borderBottom:collapsed?"none":`1px solid ${d.success?t.ok:t.err}22`,cursor:"pointer",userSelect:"none"}}>
                      <span style={{display:"inline-block",transition:"transform .15s",transform:collapsed?"rotate(-90deg)":"rotate(0)",color:d.success?t.ok:t.err,fontSize:10}}>{"\u25BE"}</span>
                      <span>{d.success?"\u2705":"\u274C"}</span>
                      <span style={{fontWeight:600,color:d.success?t.ok:t.err,fontSize:11}}>{d.language||"code"} output</span>
                      {d.exec_time&&<span style={{color:t.mut,marginLeft:"auto",fontSize:10}}>{parseFloat(d.exec_time).toFixed(1)}s</span>}
                    </div>
                    {!collapsed&&d.stdout&&<pre style={{margin:0,padding:"8px 12px",whiteSpace:"pre-wrap",color:t.dim,fontSize:12,lineHeight:1.5,borderBottom:d.stderr?`1px solid ${t.brd}22`:"none",fontFamily:font}}>{d.stdout}</pre>}
                    {!collapsed&&d.stderr&&<pre style={{margin:0,padding:"8px 12px",whiteSpace:"pre-wrap",color:t.err,fontSize:11,lineHeight:1.5,background:`${t.err}08`,fontFamily:font}}>{d.stderr}</pre>}
                  </div>;});})()}
                  {/* KB sources strip — numbered citation sources for this reply */}
                  {(()=>{
                    if(isU||msg.isS||isDaedalusOutput)return null;
                    const co=renderOpts;
                    if(!co?.citations?.length)return null;
                    if(co.quickSearch&&co.citations.every(s2=>s2?.kind==="quick_search"))return null;
                    return <Collapsible theme={t} font={font} summary={`Sources (${co.citations.length})`}>
                      <div style={{display:"flex",flexDirection:"column",gap:6}}>
                        {co.citations.map(s2=><div key={s2.n} style={{display:"flex",gap:8,alignItems:"flex-start",padding:"6px 8px",background:`${t.surface}44`,border:`1px solid ${t.brd}33`,borderRadius:6}}>
                          <span style={{fontWeight:700,color:t.acc,fontSize:11,flexShrink:0}}>[{s2.n}]</span>
                          <div style={{minWidth:0}}>
                            <div style={{fontSize:11,fontWeight:600,color:t.text}}>📄 {s2.filename} <span style={{color:t.mut,fontWeight:400,fontSize:9}}>chunk {s2.chunk_index}{typeof s2.score==="number"?` · ${Math.round(s2.score*100)}% relevance`:""}</span></div>
                            {s2.snippet&&<div style={{fontSize:10,color:t.dim,marginTop:2,overflow:"hidden",display:"-webkit-box",WebkitLineClamp:2,WebkitBoxOrient:"vertical"}}>{s2.snippet}</div>}
                          </div>
                        </div>)}
                      </div>
                    </Collapsible>;
                  })()}
                  {!isU&&!isDaedalusOutput&&quickSearchPayload&&<QuickSearchSourcesPanel payload={quickSearchPayload} t={t} font={font}/>}
                  {!msg.isS&&!isEditing&&<div style={msgToolbarS}>
                    {!isU&&msg.content&&<button onClick={()=>cp(renderedContent,mid)} style={msgActionS(copied===mid?t.ok:t.mut)}>
                      {copied===mid?<><IC.Check/> copied</>:<><IC.Copy/> copy</>}
                    </button>}
                    {!isU&&msg.content&&ttsUrl&&(()=>{const generating=ttsLoadingMid===mid,playing=speakingMid===mid;return <button onClick={()=>speak(renderedContent,mid)} title={(playing||generating)?"Stop playback":"Read aloud"} style={msgActionS(playing?t.acc:generating?t.warm:t.mut)}>
                      {generating?<><span style={{width:10,height:10,border:`2px solid ${t.warm}44`,borderTopColor:t.warm,borderRadius:"50%",display:"inline-block",animation:"spin 1s linear infinite"}}/> {ttsPhase||"generating"}</>:playing?<><IC.Stop/> playing</>:<><IC.Volume/> speak</>}
                    </button>;})()}
                    {!isU&&msg.content&&!streaming&&<div style={{position:"relative",display:"inline-flex"}}>
                      <button onClick={()=>regenerate(i)} style={{...msgActionS(t.mut),borderRadius:"5px 0 0 5px"}}>
                        <IC.Refresh/> regenerate
                      </button>
                      <button onClick={()=>setRegenPopover(p=>p?.index===i?null:{index:i,model:"",temperature:"",personaId:""})} title="Regenerate with different model / temperature / profile" style={{...msgActionS(regenPopover?.index===i?t.acc:t.mut),padding:"3px 5px",borderRadius:"0 5px 5px 0"}}>▾</button>
                      {regenPopover?.index===i&&<div style={{position:"absolute",top:"calc(100% + 4px)",left:0,background:t.bgDeep,border:`1px solid ${t.brd}55`,borderRadius:8,padding:10,zIndex:20,minWidth:240,boxShadow:`0 8px 24px rgba(0,0,0,.4)`}}>
                        <div style={{fontSize:10,color:t.mut,textTransform:"uppercase",letterSpacing:1,marginBottom:8,fontWeight:700}}>Regenerate with</div>
                        <div style={{marginBottom:6}}>
                          <div style={{fontSize:9,color:t.mut,marginBottom:2}}>Model</div>
                          <select value={regenPopover.model} onChange={e=>setRegenPopover(p=>({...p,model:e.target.value}))} style={{width:"100%",fontSize:11,padding:"4px 6px",background:t.bgDeep,border:`1px solid ${t.brd}55`,color:t.text,borderRadius:4,fontFamily:font,boxSizing:"border-box"}}>
                            <option value="">(default: {act?.model||"current"})</option>
                            {models.map(m=><option key={m} value={m}>{m}</option>)}
                          </select>
                        </div>
                        <div style={{marginBottom:6}}>
                          <div style={{fontSize:9,color:t.mut,marginBottom:2}}>Temperature</div>
                          <input type="number" min="0" max="2" step="0.05" placeholder={`default: ${defaultTemp}`} value={regenPopover.temperature} onChange={e=>setRegenPopover(p=>({...p,temperature:e.target.value}))} style={{width:"100%",fontSize:11,padding:"4px 6px",background:t.bgDeep,border:`1px solid ${t.brd}55`,color:t.text,borderRadius:4,fontFamily:font,boxSizing:"border-box"}}/>
                        </div>
                        <div style={{marginBottom:8}}>
                          <div style={{fontSize:9,color:t.mut,marginBottom:2}}>Profile</div>
                          <select value={regenPopover.personaId} onChange={e=>setRegenPopover(p=>({...p,personaId:e.target.value}))} style={{width:"100%",fontSize:11,padding:"4px 6px",background:t.bgDeep,border:`1px solid ${t.brd}55`,color:t.text,borderRadius:4,fontFamily:font,boxSizing:"border-box"}}>
                            <option value="">(default: current)</option>
                            <option value="none">-- No profile --</option>
                            <optgroup label="Agents">
                              {mcs.filter(isAgentProfile).map(mc=><option key={mc.id} value={mc.id}>{mc.name}</option>)}
                            </optgroup>
                            <optgroup label="Personas">
                              {mcs.filter(isPersonaProfile).map(mc=><option key={mc.id} value={mc.id}>{mc.name}</option>)}
                            </optgroup>
                          </select>
                        </div>
                        <div style={{display:"flex",gap:4,justifyContent:"flex-end"}}>
                          <button onClick={()=>setRegenPopover(null)} style={{background:"none",border:`1px solid ${t.brd}44`,color:t.mut,cursor:"pointer",padding:"3px 10px",borderRadius:4,fontFamily:font,fontSize:10}}>Cancel</button>
                          <button onClick={()=>{
                            const ov={};
                            if(regenPopover.model)ov.model=regenPopover.model;
                            if(regenPopover.temperature!==""){const n=parseFloat(regenPopover.temperature);if(!isNaN(n))ov.temperature=n;}
                            if(regenPopover.personaId)ov.persona_id=regenPopover.personaId==="none"?null:regenPopover.personaId;
                            setRegenPopover(null);
                            regenerate(i, Object.keys(ov).length?ov:undefined);
                          }} style={{background:`${t.acc}22`,border:`1px solid ${t.acc}55`,color:t.acc,cursor:"pointer",padding:"3px 10px",borderRadius:4,fontFamily:font,fontSize:10,fontWeight:700}}>Regenerate</button>
                        </div>
                      </div>}
                    </div>}
                    {!streaming&&<button onClick={()=>setEditingMsg({index:i,content:msg.content})} title={isU?"Edit and resend":"Edit response (raw)"} style={msgActionS(t.mut)}>
                      <IC.Pencil/> edit
                    </button>}
                    {!streaming&&!ghostActive&&<button onClick={()=>forkAt(i)} title="Fork conversation from this point" style={msgActionS(t.mut)}>
                      <IC.GitBranch/> fork
                    </button>}
                    {!streaming&&<button onClick={()=>delMsg(i)} title="Delete this message" style={msgActionS(t.mut)}>
                      <IC.Trash/> delete
                    </button>}
                  </div>}
                </div>
              </div></React.Fragment>;
            })}</>;})()}
            {evts.length>0&&!(act.messages||[]).some(m=>m.role==="assistant")&&<div style={{maxWidth:chatWidth,margin:"0 auto",padding:"8px 16px"}}><ToolStatus evts={evts} t={t} expandedPill={expandedPill} setExpandedPill={setExpandedPill} onPreview={openPreview} onOpenArtifact={openArtifact} md={md}/></div>}
            <div ref={chatEnd}/>
          </div>}
        </div>
        </div>
        <div style={{flexShrink:0,position:"relative",transform:emptyComposerLift,transition:"transform .7s cubic-bezier(.2,.8,.2,1)",willChange:"transform",zIndex:120,pointerEvents:"auto"}}>
        {/* TOOL TOGGLES BAR + QUICK SEARCH */}
        <div style={{padding:"0 16px",flexShrink:0}}>
          <div ref={connectorPickerRef} style={{maxWidth:chatWidth,margin:"0 auto",position:"relative"}}>
            <div style={{display:"flex",gap:6,overflowX:"auto",padding:"6px 0",alignItems:"center",minHeight:32}}>
              {!act?.is_council&&composerToolChips.map(tl=>{
                const on=activeToolIds.includes(tl.id);
                const isQs=tl.id==="quick_search";
                const col=isQs?t.f1:tl.connector?t.acc:t.warm;
                return <button key={tl.id} title={tl.description||tl.name} onClick={()=>toggleToolId(tl.id)} style={{display:"flex",alignItems:"center",gap:4,padding:"4px 10px",borderRadius:8,fontSize:10,fontWeight:on?700:500,cursor:"pointer",whiteSpace:"nowrap",background:on?`${col}18`:`${t.surface}D9`,border:`1px solid ${on?col:t.brd}44`,color:on?col:t.mut,transition:"all .15s",boxShadow:"none",maxWidth:tl.connector?180:"none",overflow:"hidden",textOverflow:"ellipsis",flexShrink:0}}>
                  {isQs&&searchLoading?<span style={{display:"inline-block",animation:"spin .8s linear infinite",flexShrink:0}}>⟳</span>:(on?<IC.ToggleOn/>:<IC.ToggleOff/>)}<span style={{overflow:"hidden",textOverflow:"ellipsis"}}>{tl.name}</span>
                </button>;
              })}
              {!act?.is_council&&connectorOperationTools.length>0&&<button onClick={()=>{setShowConnectorPicker(p=>!p);setConnectorSearch("");}} title="Connector tools" style={{display:"flex",alignItems:"center",gap:4,padding:"4px 10px",borderRadius:8,fontSize:10,fontWeight:showConnectorPicker||activeConnectorCount?800:600,cursor:"pointer",whiteSpace:"nowrap",background:showConnectorPicker||activeConnectorCount?`${t.acc}18`:`${t.surface}D9`,border:`1px solid ${showConnectorPicker||activeConnectorCount?t.acc:t.brd}44`,color:showConnectorPicker||activeConnectorCount?t.acc:t.mut,transition:"all .15s",boxShadow:"none",flexShrink:0}}>
                <IC.Tool/> Connectors{activeConnectorCount?` ${activeConnectorCount}`:""}
              </button>}
            </div>
            {!act?.is_council&&showConnectorPicker&&connectorOperationTools.length>0&&<div style={{position:"absolute",bottom:"calc(100% + 6px)",left:0,zIndex:310,width:300,maxWidth:"calc(100vw - 32px)",maxHeight:260,overflow:"hidden",background:`${t.bgDeep}F7`,border:`1px solid ${t.brd}55`,borderRadius:10,boxShadow:`0 10px 28px #0009`,padding:8,display:"flex",flexDirection:"column",gap:7,backdropFilter:"blur(8px)"}}>
              <div style={{display:"flex",alignItems:"center",gap:7}}>
                <div style={{fontSize:10,fontWeight:900,color:t.acc,textTransform:"uppercase",letterSpacing:.7,whiteSpace:"nowrap"}}>Connectors</div>
                <div style={{fontSize:9,color:t.mut,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",flex:1}}>{activeConnectorCount} active · {connectorOperationTools.length} available</div>
                <button onClick={()=>setShowConnectorPicker(false)} title="Close" style={{background:"transparent",border:"none",color:t.mut,cursor:"pointer",padding:2,fontSize:13,lineHeight:1}}>×</button>
              </div>
              <input value={connectorSearch} onChange={e=>setConnectorSearch(e.target.value)} placeholder="Search connector tools" style={{...inputS,fontSize:10,padding:"6px 8px",height:30,boxSizing:"border-box"}}/>
              <div style={{overflowY:"auto",display:"flex",flexDirection:"column",gap:3,paddingRight:2}}>
                {filteredConnectorTools.slice(0,80).map(tl=>{const on=activeToolIds.includes(tl.id);const label=String(tl.name||"").replace(/^([^:]{1,28}):\s*/,"");return <button key={tl.id} onClick={()=>toggleToolId(tl.id)} title={`${tl.name}\n${tl.description||""}`} style={{display:"grid",gridTemplateColumns:"16px 1fr",gap:6,alignItems:"center",width:"100%",minHeight:29,padding:"5px 7px",borderRadius:7,border:`1px solid ${on?t.acc:t.brd}22`,background:on?`${t.acc}13`:`${t.surface}55`,color:on?t.acc:t.dim,cursor:"pointer",fontFamily:"system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif",textAlign:"left",boxSizing:"border-box"}}>
                  <span style={{display:"flex",color:on?t.acc:t.mut,transform:"scale(.82)"}}>{on?<IC.ToggleOn/>:<IC.ToggleOff/>}</span>
                  <span style={{minWidth:0,display:"block",fontSize:10,fontWeight:on?750:600,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",lineHeight:1.2}}>{label}</span>
                </button>;})}
                {filteredConnectorTools.length>80&&<div style={{fontSize:9,color:t.mut,textAlign:"center",padding:6}}>Showing first 80. Search to narrow.</div>}
                {!filteredConnectorTools.length&&<div style={{fontSize:10,color:t.mut,textAlign:"center",padding:14}}>No connector tools match.</div>}
              </div>
            </div>}
          </div>
        </div>
        {/* INPUT */}
        <div style={{padding:"6px 18px 16px",flexShrink:0}}>
          {currentRun&&(()=>{
            const phaseLabel={searching:"searching",thinking:"thinking",tool:"tool running",streaming:"streaming",voting:"council voting",council:"council",stopped:"stopped",failed:"failed",complete:"complete"}[currentRun.phase]||currentRun.phase||"running";
            const pct=currentRun.pct!=null?Math.max(0,Math.min(100,Number(currentRun.pct)||0)):null;
            const active=streaming||councilRunning||searchLoading;
            return <div style={{maxWidth:chatWidth,margin:"0 auto 7px",display:"flex",alignItems:"center",gap:8,padding:"6px 10px",borderRadius:8,background:`${currentRun.color}0d`,border:`1px solid ${currentRun.color}2f`,color:currentRun.color,fontSize:11,minHeight:31,boxSizing:"border-box"}}>
              <span style={{width:7,height:7,borderRadius:"50%",background:currentRun.color,boxShadow:active?`0 0 9px ${currentRun.color}88`:"none",animation:active?"pulse 1.5s infinite":"none",flexShrink:0}}/>
              <span style={{fontWeight:800,textTransform:"uppercase",letterSpacing:.6,fontSize:9,whiteSpace:"nowrap"}}>{phaseLabel}</span>
              <span style={{color:t.text,fontWeight:700,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{currentRun.label}</span>
              {currentRun.detail&&<span style={{color:t.mut,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",flex:1,minWidth:40}}>{currentRun.detail}</span>}
              {pct!=null&&<span style={{fontSize:9,color:t.dim,whiteSpace:"nowrap"}}>{pct}%</span>}
              {streaming&&<button onClick={stop} title="Stop generation" style={{background:"none",border:`1px solid ${currentRun.color}35`,color:currentRun.color,cursor:"pointer",padding:"2px 7px",borderRadius:6,fontFamily:font,fontSize:9,fontWeight:700}}>Stop</button>}
            </div>;
          })()}
          {/* Attachment chips */}
          {quickSearchError&&<div style={{maxWidth:chatWidth,margin:"0 auto",padding:"0 0 6px",display:"flex",justifyContent:"flex-start"}}>
            <span style={{display:"inline-flex",alignItems:"center",gap:7,padding:"4px 9px",borderRadius:8,background:`${t.warm}10`,border:`1px solid ${t.warm}30`,color:t.warm,fontSize:10,fontWeight:700,maxWidth:"100%"}}>
              <span style={{width:6,height:6,borderRadius:"50%",background:t.warm,flexShrink:0}}/>
              Quick Search
              <span style={{color:t.mut,fontWeight:500,maxWidth:260,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{quickSearchError}</span>
              <button onClick={()=>setQuickSearchError(null)} style={{background:"none",border:"none",color:t.warm,cursor:"pointer",fontSize:11,padding:0,lineHeight:1}}>×</button>
            </span>
          </div>}
          {/* Council suggested prompts */}
          {act?.is_council&&!councilRunning&&(councilSuggestions.length>0||councilSugLoading)&&<div style={{maxWidth:chatWidth,margin:"0 auto",padding:"0 0 6px",display:"flex",gap:6,flexWrap:"wrap",alignItems:"center"}}>
              {councilSugLoading?<div style={{display:"flex",alignItems:"center",gap:5,padding:"4px 0",width:"100%"}}>
                <div style={{display:"flex",gap:3}}>{[0,1,2].map(i=><div key={i} style={{width:4,height:4,borderRadius:"50%",background:t.pink,animation:`pulse 1.4s ${i*.16}s infinite`}}/>)}</div>
                <span style={{fontSize:10,color:t.mut}}>Generating suggestions...</span>
              </div>:councilSuggestions.map((s,i)=><button key={i} onClick={()=>{setInp(s);setCouncilSuggestions([]);setTimeout(()=>inpRef.current?.focus(),50);}}
                style={{background:`${t.pink}08`,border:`1px solid ${t.pink}25`,borderRadius:20,padding:"5px 12px",fontSize:11,color:t.dim,cursor:"pointer",fontFamily:font,transition:"all .15s",maxWidth:"100%",textAlign:"left",lineHeight:1.4}}
                onMouseOver={e=>{e.currentTarget.style.background=`${t.pink}18`;e.currentTarget.style.borderColor=`${t.pink}44`;e.currentTarget.style.color=t.text;}}
                onMouseOut={e=>{e.currentTarget.style.background=`${t.pink}08`;e.currentTarget.style.borderColor=`${t.pink}25`;e.currentTarget.style.color=t.dim;}}>
                {s}
              </button>)}
              {!councilSugLoading&&<button onClick={()=>fetchCouncilSuggestions(act?.council_config_id)} title="Regenerate suggestions" style={{background:"none",border:`1px solid ${t.brd}33`,borderRadius:"50%",width:24,height:24,display:"flex",alignItems:"center",justifyContent:"center",cursor:"pointer",color:t.mut,fontSize:11,flexShrink:0}}>↻</button>}
            </div>}
          {attachments.length>0&&<div style={{maxWidth:chatWidth,margin:"0 auto",display:"flex",flexWrap:"wrap",gap:6,padding:"6px 0"}}>
            {attachments.map((a,i)=>a.type==="image"?
              <span key={i} title={a.name} style={{display:"inline-flex",alignItems:"center",gap:6,background:`${t.acc}10`,border:`1px solid ${t.acc}30`,padding:"3px 6px 3px 3px",borderRadius:8,fontSize:10,fontWeight:600,maxWidth:260,minHeight:30,boxSizing:"border-box"}}>
                <img src={a.dataUrl} alt={a.name} style={{height:26,width:26,objectFit:"cover",borderRadius:5,border:`1px solid ${t.acc}33`,display:"block",cursor:"pointer",flexShrink:0}} onClick={()=>openPreview&&openPreview(a.name,a.dataUrl)}/>
                <span style={{color:t.text,fontWeight:500,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",maxWidth:140}}>{a.name}</span>
                <span style={{color:t.mut,fontWeight:400,fontSize:9}}>{(a.size/1024).toFixed(0)}KB</span>
                <button onClick={()=>setAttachments(p=>p.filter((_,j)=>j!==i))} title="Remove attachment" style={{background:"none",border:"none",color:t.acc,cursor:"pointer",padding:"1px 3px",fontSize:12,opacity:.78,lineHeight:1}}>&times;</button>
              </span>
            :a.type==="pdf"?
              <span key={i} style={{display:"inline-flex",alignItems:"center",gap:6,background:a.loading?`${t.warm}12`:a.error?`${t.err}10`:`${t.acc}10`,border:`1px solid ${a.loading?t.warm:a.error?t.err:t.acc}30`,color:a.loading?t.warm:a.error?t.err:t.acc,padding:"4px 7px",borderRadius:8,fontSize:10,fontWeight:600,transition:"all .2s",maxWidth:"100%",minHeight:30,boxSizing:"border-box"}}>
                <span style={{fontSize:13}}>{a.loading?"⏳":"📄"}</span>
                <span style={{color:t.text,fontWeight:500,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",maxWidth:180}}>{a.name}</span>
                {!a.loading&&!a.error&&a.pages>0&&<span style={{color:t.mut,fontWeight:400}}>{a.pages}p</span>}
                {a.loading&&<span style={{color:t.mut,fontWeight:400,fontStyle:"italic"}}>extracting...</span>}
                {a.error&&<span style={{color:t.err,fontWeight:500,fontSize:9,maxWidth:220,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{a.error}</span>}
                {a.error&&a.file&&<button onClick={()=>{const f=a.file;setAttachments(p=>p.filter((_,j)=>j!==i));handleFileUpload([f]);}} style={{background:"none",border:`1px solid ${t.acc}55`,color:t.acc,cursor:"pointer",padding:"2px 7px",borderRadius:6,fontFamily:font,fontSize:9,fontWeight:700}}>Retry</button>}
                <button onClick={()=>setAttachments(p=>p.filter((_,j)=>j!==i))} style={{background:"none",border:"none",color:a.error?t.err:t.mut,cursor:"pointer",padding:"1px 4px",fontSize:12,opacity:.8}}>&times;</button>
              </span>
            :<span key={i} title={a.name} style={{display:"inline-flex",alignItems:"center",gap:6,background:`${t.f1}10`,border:`1px solid ${t.f1}30`,color:t.f1,padding:"4px 7px",borderRadius:8,fontSize:10,fontWeight:600,maxWidth:260,minHeight:30,boxSizing:"border-box"}}>
                <IC.Paperclip/>
                <span style={{color:t.text,fontWeight:500,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",maxWidth:190}}>{a.name}</span>
                <button onClick={()=>setAttachments(p=>p.filter((_,j)=>j!==i))} title="Remove attachment" style={{background:"none",border:"none",color:t.f1,cursor:"pointer",padding:"1px 3px",fontSize:12,opacity:.78,lineHeight:1}}>&times;</button>
              </span>)}
          </div>}
          <div className={isEmptyChatSurface?"empty-composer-box":""} style={{maxWidth:chatWidth,margin:"0 auto",...glass,background:composerState==="error"?`${t.err}08`:composerState==="stopped"?`${t.surface}E8`:glass.background,borderRadius:8,padding:isEmptyChatSurface?"9px 7px 9px 14px":"6px 6px 6px 12px",display:"flex",alignItems:"center",gap:6,border:`1px solid ${composerColor}${composerActive||composerFocused?"55":"32"}`,boxShadow:composerActive?`0 0 10px ${composerColor}18`:composerFocused?`0 0 0 2px ${composerColor}12`:"none",transition:"border-color .18s, box-shadow .22s, background .18s",minHeight:isEmptyChatSurface?60:48,position:"relative",overflow:"visible",isolation:"isolate","--empty-composer-glow":`${composerColor}24`}}>
            {composerActive&&<div aria-hidden="true" style={{position:"absolute",inset:0,borderRadius:8,pointerEvents:"none",overflow:"hidden",zIndex:0}}>
              {["top","right","bottom","left"].map(side=><span key={side} className={`composer-trace composer-trace-${side}`} style={{"--trace-color":composerColor}}/>)}
            </div>}
            {!act?.is_council&&<><div ref={quickMenuRef} style={{position:"relative",flexShrink:0}}>
              <button onClick={()=>{setShowQuickMenu(p=>!p);setShowPromptPicker(false);}} title="Quick actions" style={{background:showQuickMenu?`${t.acc}18`:"none",border:showQuickMenu?`1px solid ${t.acc}44`:"none",color:showQuickMenu?t.acc:t.mut,cursor:"pointer",padding:"4px 6px",display:"flex",alignItems:"center",justifyContent:"center",flexShrink:0,opacity:showQuickMenu?1:.75,borderRadius:7,fontSize:17,lineHeight:1}}><IC.Plus/></button>
              {showQuickMenu&&<div style={{position:"absolute",bottom:"115%",left:0,zIndex:300,background:t.bgDeep,border:`1px solid ${t.brd}44`,borderRadius:12,boxShadow:`0 4px 24px #0008`,minWidth:220,padding:7,display:"flex",flexDirection:"column",gap:4}}>
                <button onClick={()=>{fileRef.current?.click();setShowQuickMenu(false);}} style={{display:"flex",alignItems:"center",gap:8,width:"100%",padding:"8px 10px",borderRadius:8,border:"none",background:`${t.surface}66`,color:t.dim,cursor:"pointer",fontFamily:font,fontSize:12,textAlign:"left"}}><IC.Paperclip/> Attach files</button>
                {prompts.length>0&&<button onClick={()=>{setShowPromptPicker(true);setPromptSearch("");setShowQuickMenu(false);}} style={{display:"flex",alignItems:"center",gap:8,width:"100%",padding:"8px 10px",borderRadius:8,border:"none",background:showPromptPicker?`${t.f1}18`:`${t.surface}66`,color:showPromptPicker?t.f1:t.dim,cursor:"pointer",fontFamily:font,fontSize:12,textAlign:"left"}}>⚡ Prompt Library</button>}
                {(()=>{const coderMc=mcs.find(m=>isCoderPersonaName(m.name));const isCoderActive=isCoderPersonaName(act?.persona_name)||(!actId&&isCoderPersonaName(pendingPersona?.persona_name));return coderMc?<button onClick={()=>{setShowQuickMenu(false);if(!actId){if(isCoderActive){setPendingPersona(null);setPendingToolIds([]);setLastPersonaId(null);localStorage.removeItem("hc-last-persona");return;}const persona={model:coderMc.base_model||models[0]||"qwen3.5:27b",system_prompt:coderMc.system_prompt,tool_ids:coderMc.tool_ids||[],model_config_id:coderMc.id,persona_name:coderMc.name,persona_avatar:profileAvatar(coderMc)};modelChoiceRef.current.pending=persona.model||"";setPendingPersona(persona);setPendingToolIds(persona.tool_ids||[]);setLastPersonaId(coderMc.id);localStorage.setItem("hc-last-persona",coderMc.id);return;}if(isCoderActive){uConv(actId,{model_config_id:null,persona_name:null,persona_avatar:null,system_prompt:"",tool_ids:[]});setLastPersonaId(null);localStorage.removeItem("hc-last-persona");return;}uConv(actId,{model:coderMc.base_model||act?.model,system_prompt:coderMc.system_prompt,tool_ids:coderMc.tool_ids||[],model_config_id:coderMc.id,persona_name:coderMc.name,persona_avatar:profileAvatar(coderMc)});setLastPersonaId(coderMc.id);localStorage.setItem("hc-last-persona",coderMc.id);}} style={{display:"flex",alignItems:"center",gap:8,width:"100%",padding:"8px 10px",borderRadius:8,border:"none",background:isCoderActive?`${t.ok}18`:`${t.surface}66`,color:isCoderActive?t.ok:t.dim,cursor:"pointer",fontFamily:font,fontSize:12,textAlign:"left"}}>&lt;/&gt; {isCoderActive?"Disable Daedalus":"Activate Daedalus"}</button>:null;})()}
              </div>}
            </div>
            <input ref={fileRef} type="file" multiple style={{display:"none"}} onChange={e=>{if(e.target.files?.length)handleFileUpload(Array.from(e.target.files));e.target.value="";}}/>
            {prompts.length>0&&<div ref={promptPickerRef} style={{position:"relative",flexShrink:0}}>
              {showPromptPicker&&<div style={{position:"absolute",bottom:"110%",left:0,zIndex:310,background:t.bgDeep,border:`1px solid ${t.brd}44`,borderRadius:12,boxShadow:`0 4px 24px #0008`,minWidth:280,maxWidth:360,padding:8}}>
                <input value={promptSearch} onChange={e=>setPromptSearch(e.target.value)} placeholder="Search prompts..." style={{...inputS,marginBottom:6,padding:"5px 8px",fontSize:11}} autoFocus/>
                <div style={{maxHeight:220,overflowY:"auto",display:"flex",flexDirection:"column",gap:2}}>
                  {prompts.filter(p=>!promptSearch||p.title.toLowerCase().includes(promptSearch.toLowerCase())||p.content.toLowerCase().includes(promptSearch.toLowerCase())).map(p=><div key={p.id} onClick={(e)=>{e.stopPropagation();setInp(p.content);setShowPromptPicker(false);setShowQuickMenu(false);setTimeout(()=>{if(inpRef.current){inpRef.current.style.height="auto";inpRef.current.style.height=Math.min(inpRef.current.scrollHeight,120)+"px";inpRef.current.focus();}},50);}} style={{padding:"7px 10px",borderRadius:8,cursor:"pointer",background:`${t.surface}66`,border:`1px solid ${t.brd}22`,transition:"background .12s"}} onMouseEnter={e=>e.currentTarget.style.background=`${t.f1}15`} onMouseLeave={e=>e.currentTarget.style.background=`${t.surface}66`}>
                    <div style={{fontSize:11,fontWeight:600,color:t.text}}>{p.title}</div>
                    <div style={{fontSize:9,color:t.f1,marginTop:1}}>{p.category||"General"}</div>
                  </div>)}
                  {prompts.filter(p=>!promptSearch||p.title.toLowerCase().includes(promptSearch.toLowerCase())).length===0&&<div style={{textAlign:"center",padding:16,color:t.mut,fontSize:11}}>No matches</div>}
                </div>
                <div style={{borderTop:`1px solid ${t.brd}22`,marginTop:4,paddingTop:4}}>
                  <button onClick={()=>{setShowPromptPicker(false);setShowQuickMenu(false);setPanel("prompts");}} style={{...btnS(t.f1),width:"100%",justifyContent:"center",fontSize:9}}>Manage Prompts →</button>
                </div>
              </div>}
            </div>}
            </>}
            <textarea ref={inpRef} value={inp} onChange={e=>setInp(e.target.value)} onKeyDown={e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send();}}}
              placeholder={`${act?.is_council&&councilRunning?"⚖️ Council is deliberating...":act?.is_council?"Bring forth your query, the council awaits...":"What's on your mind?"}`} rows={1}
              disabled={streaming||councilRunning||loadingConv}
              style={{flex:1,background:"transparent",border:"none",color:t.text,fontFamily:font,fontSize:14,outline:"none",resize:"none",padding:isEmptyChatSurface?"11px 0":"8px 0",minHeight:isEmptyChatSurface?38:"auto",maxHeight:140,lineHeight:1.6}}
              onFocus={()=>setComposerFocused(true)} onBlur={()=>setComposerFocused(false)}
              onInput={e=>{e.target.style.height="auto";e.target.style.height=Math.min(e.target.scrollHeight,120)+"px";}}
              onPaste={e=>{const files=Array.from(e.clipboardData?.files||[]);if(files.length){e.preventDefault();handleFileUpload(files);}}}/>
            {!act?.is_council&&(()=>{const lvl=resolveEffort(actId);const L=EFFORT_LEVELS[lvl]||EFFORT_LEVELS[0];const overridden=actId?effortPerChat[actId]!==undefined&&effortPerChat[actId]!==globalEffort:pendingEffort!==null&&pendingEffort!==globalEffort;return <div ref={effortPickerRef} style={{position:"relative",flexShrink:0}}>
              <button onClick={()=>setShowEffortPicker(p=>!p)} title={`Effort: ${L.name} — ${L.desc}${overridden?" (overrides global)":""}`} style={{background:showEffortPicker?`${t.pink}15`:"none",border:overridden?`1px solid ${t.pink}55`:"none",color:lvl>0?t.pink:t.mut,cursor:"pointer",padding:"3px 8px",borderRadius:8,display:"flex",alignItems:"center",gap:4,fontSize:11,flexShrink:0,opacity:lvl>0?1:.65,fontWeight:600}}>
                <span style={{fontSize:13}}>{L.emoji}</span><span style={{fontSize:10}}>{L.name}</span>
              </button>
              {showEffortPicker&&<div style={{position:"absolute",bottom:"110%",right:0,zIndex:300,background:t.bgDeep,border:`1px solid ${t.pink}33`,borderRadius:10,boxShadow:`0 4px 24px #0008`,minWidth:200,padding:6}}>
                <div style={{fontSize:9,color:t.mut,textTransform:"uppercase",letterSpacing:.5,padding:"3px 6px 5px",fontWeight:700}}>Effort — this chat</div>
                {EFFORT_LEVELS.map((lv,v)=><button key={v} onClick={()=>{if(actId){setEffortPerChat(p=>({...p,[actId]:v}));}else{setPendingEffort(v);}setShowEffortPicker(false);}} style={{display:"flex",alignItems:"center",gap:6,width:"100%",textAlign:"left",padding:"6px 8px",borderRadius:6,background:lvl===v?`${t.pink}18`:"transparent",border:"none",color:lvl===v?t.pink:t.dim,cursor:"pointer",fontSize:11,fontFamily:font,marginBottom:2}}>
                  <span style={{fontSize:14}}>{lv.emoji}</span>
                  <div style={{flex:1,minWidth:0}}>
                    <div style={{fontWeight:600}}>{lv.name}{globalEffort===v&&<span style={{fontSize:8,color:t.mut,marginLeft:4,opacity:.7}}>default</span>}</div>
                    <div style={{fontSize:9,color:t.mut}}>{lv.desc}</div>
                  </div>
                </button>)}
                {((actId&&effortPerChat[actId]!==undefined)||(!actId&&pendingEffort!==null))&&<div style={{borderTop:`1px solid ${t.brd}22`,marginTop:4,paddingTop:4}}>
                  <button onClick={()=>{if(actId){setEffortPerChat(p=>{const n={...p};delete n[actId];return n;});}else{setPendingEffort(null);}setShowEffortPicker(false);}} style={{width:"100%",background:"none",border:"none",color:t.mut,cursor:"pointer",fontSize:10,padding:"4px 0",fontFamily:font}}>Use global default →</button>
                </div>}
              </div>}
            </div>;})()}
            {sttUrl&&!streaming&&!councilRunning&&<button onClick={toggleRecording} disabled={transcribing} title={recording?"Stop recording":transcribing?"Transcribing…":"Voice input (speech-to-text)"} style={{background:recording?`${t.err}22`:"none",border:recording?`1px solid ${t.err}66`:"1px solid transparent",color:recording?t.err:transcribing?t.warm:t.mut,cursor:transcribing?"default":"pointer",padding:"6px 8px",borderRadius:8,display:"flex",alignItems:"center",flexShrink:0,animation:recording?"pGlow 1.5s ease-in-out infinite":"none"}}>
              {transcribing?<span style={{width:13,height:13,border:`2px solid ${t.warm}44`,borderTopColor:t.warm,borderRadius:"50%",display:"inline-block",animation:"spin 1s linear infinite"}}/>:<IC.Mic/>}
            </button>}
            {councilRunning?<button onClick={()=>setCouncilRunning(false)} style={{background:t.pink,border:"none",color:"#fff",padding:"10px 14px",borderRadius:8,cursor:"pointer",display:"flex",alignItems:"center",justifyContent:"center",flexShrink:0,animation:"pCouncilGlow 1.5s ease-in-out infinite"}}><IC.Stop/></button>
            :streaming?<button onClick={stop} style={{background:t.err,border:"none",color:"#fff",padding:"10px 14px",borderRadius:8,cursor:"pointer",display:"flex",alignItems:"center",justifyContent:"center",flexShrink:0,animation:"pGlow 1.5s ease-in-out infinite"}}><IC.Stop/></button>
            :<button onClick={send} disabled={!inp.trim()&&!attachments.length} title={(inp.trim()||attachments.length)?"Send message":"Type a message or attach a file"} style={{background:(inp.trim()||attachments.length)?(act?.is_council?t.pink:t.warm):`${t.sfBri}88`,border:`1px solid ${(inp.trim()||attachments.length)?(act?.is_council?t.pink:t.warm):t.brd}22`,color:(inp.trim()||attachments.length)?"#fff":t.mut,padding:"10px 14px",borderRadius:8,cursor:(inp.trim()||attachments.length)?"pointer":"default",display:"flex",alignItems:"center",justifyContent:"center",flexShrink:0,transition:"all .2s",boxShadow:"none",opacity:(inp.trim()||attachments.length)?1:.62}}>{act?.is_council?<IC.Council/>:<IC.Send/>}</button>}
          </div>
        </div>
        </div>
      </>}
      </div>{/* end inner panel column */}
      {/* Preview panel — outside panel ternary so it renders alongside any panel */}
      {previewFile&&<div style={{width:previewWidth,flexShrink:0,borderLeft:`1px solid ${t.brd}33`,background:t.bgDeep,display:"flex",flexDirection:"column",position:"relative",overflow:"hidden",transition:"width .15s"}}>
            {/* Drag handle */}
            <div onMouseDown={startPreviewDrag} style={{position:"absolute",left:0,top:0,bottom:0,width:5,cursor:"col-resize",zIndex:10,background:"transparent"}} onMouseEnter={e=>e.currentTarget.style.background=`${t.acc}44`} onMouseLeave={e=>e.currentTarget.style.background="transparent"}/>
            {/* Header */}
            <div style={{padding:"9px 12px",borderBottom:`1px solid ${t.brd}22`,display:"flex",alignItems:"center",gap:7,flexShrink:0,background:`${t.surface}88`}}>
              <span style={{fontSize:11,fontWeight:700,color:t.text,flex:1,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{previewFile.isExternal?(()=>{try{return new URL(previewFile.url).hostname.replace("www.","");}catch{return previewFile.filename;}})():previewFile.filename}</span>
              <span style={{fontSize:9,color:previewFile.isExternal?t.warm:t.mut,background:`${t.surface}`,padding:"2px 6px",borderRadius:4,flexShrink:0}}>{previewFile.isExternal?"web":`.${previewFile.type}`}</span>
              {previewFile.isExternal
                ?<a href={previewFile.url} target="_blank" rel="noopener" style={{color:t.acc,fontSize:10,textDecoration:"none",padding:"2px 6px",border:`1px solid ${t.acc}44`,borderRadius:4,flexShrink:0}}>↗ open</a>
                :<a href={`${API}${previewFile.url}`} download={previewFile.filename} style={{color:t.ok,fontSize:10,textDecoration:"none",padding:"2px 6px",border:`1px solid ${t.ok}44`,borderRadius:4,flexShrink:0}}>⬇</a>}
              <button onClick={closePreview} style={{background:"none",border:"none",color:t.mut,cursor:"pointer",fontSize:16,padding:"0 2px",flexShrink:0,lineHeight:1}}>✕</button>
            </div>
            {/* Content */}
            <div style={{flex:1,overflowY:"auto",padding:14}}>
              {previewError?<div style={{display:"flex",flexDirection:"column",gap:10,padding:16,background:`${t.err}10`,border:`1px solid ${t.err}33`,borderRadius:10,color:t.err}}>
                <div style={{fontWeight:700,fontSize:12}}>⚠ {previewError}</div>
                {previewFile.isExternal
                  ?<a href={previewFile.url} target="_blank" rel="noopener" style={{color:t.acc,fontSize:11,textDecoration:"none",padding:"5px 10px",border:`1px solid ${t.acc}44`,borderRadius:6,display:"inline-flex",alignItems:"center",gap:4}}>↗ Open in new tab</a>
                  :<a href={`${API}${previewFile.url}`} download={previewFile.filename} style={{color:t.ok,fontSize:11,textDecoration:"none",padding:"5px 10px",border:`1px solid ${t.ok}44`,borderRadius:6,display:"inline-flex",alignItems:"center",gap:4}}>⬇ Download anyway</a>}
              </div>
              :previewFile.isExternal&&previewFile.type==="pdf"
                ?<iframe src={`${API}/api/proxy-preview?url=${encodeURIComponent(previewFile.url)}`} style={{width:"100%",height:"100%",border:"none",borderRadius:6,minHeight:500}} title={previewFile.filename}/>
              :previewFile.isExternal&&["png","jpg","jpeg","gif","webp","svg"].includes(previewFile.type)
                ?<img src={`${API}/api/proxy-preview?url=${encodeURIComponent(previewFile.url)}`} style={{maxWidth:"100%",borderRadius:6,display:"block"}} alt={previewFile.filename}/>
              :["png","jpg","jpeg","gif","webp","svg"].includes(previewFile.type)&&!previewFile.isExternal
                ?<img src={`${API}${previewFile.url}`} style={{maxWidth:"100%",borderRadius:6,display:"block"}} alt={previewFile.filename}/>
                :previewFile.type==="archive"&&previewArchive
                ?<div>
                  <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:12,padding:"8px 12px",background:`${t.surface}88`,borderRadius:8,border:`1px solid ${t.brd}22`}}>
                    <span style={{fontSize:20}}>📦</span>
                    <div style={{flex:1}}>
                      <div style={{fontSize:12,fontWeight:700,color:t.text}}>{previewArchive.filename}</div>
                      <div style={{fontSize:10,color:t.mut}}>{previewArchive.file_count} files, {previewArchive.entries.length} entries</div>
                    </div>
                    <a href={`${API}${previewFile.url}`} download={previewFile.filename} style={{color:t.ok,fontSize:10,textDecoration:"none",padding:"4px 10px",border:`1px solid ${t.ok}44`,borderRadius:6,fontWeight:600}}>⬇ Download</a>
                  </div>
                  <div style={{borderRadius:8,border:`1px solid ${t.brd}22`,overflow:"hidden"}}>
                    {previewArchive.entries.filter(e=>e.name!=="./"&&e.name!=="."&&e.name!=="/").map((entry,i)=>{
                      const depth=Math.max(0,(entry.name.match(/\//g)||[]).length-(entry.is_dir?1:0));
                      const basename=entry.is_dir?entry.name.replace(/\/$/,"").split("/").pop()+"/":entry.name.split("/").pop();
                      const sizeStr=entry.is_dir?"":entry.size>=1048576?(entry.size/1048576).toFixed(1)+"MB":entry.size>=1024?(entry.size/1024).toFixed(1)+"KB":entry.size+"B";
                      const extC=(basename.match(/\.(\w+)$/)||[])[1]||"";
                      const iconColor=entry.is_dir?t.warm:["py","js","ts","rs","go","java","c","cpp","sh"].includes(extC)?t.acc:["md","txt","log","csv"].includes(extC)?t.dim:["json","yaml","toml","xml","html","css"].includes(extC)?t.f1:t.mut;
                      return <div key={i} style={{display:"flex",alignItems:"center",gap:6,padding:"4px 10px",paddingLeft:10+depth*16,background:i%2===0?"transparent":`${t.surface}33`,borderBottom:i<previewArchive.entries.length-1?`1px solid ${t.brd}11`:"none",fontSize:11,color:entry.is_dir?t.warm:t.dim}}>
                        <span style={{fontSize:12,flexShrink:0,color:iconColor}}>{entry.is_dir?"📁":"📄"}</span>
                        <span style={{flex:1,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",fontWeight:entry.is_dir?600:400}}>{basename}</span>
                        {sizeStr&&<span style={{fontSize:9,color:t.mut,flexShrink:0,fontFamily:"monospace"}}>{sizeStr}</span>}
                      </div>;
                    })}
                  </div>
                </div>
                :previewFile.type==="html"&&previewContent
                ?<iframe sandbox={previewFile.isExternal?"":"allow-scripts"} srcDoc={previewContent} style={{width:"100%",height:"100%",border:"none",borderRadius:6,background:"#fff",minHeight:400}} title={previewFile.filename}/>
                :["md","markdown","mdx"].includes(previewFile.type)&&previewContent
                ?<div style={{lineHeight:1.7,fontSize:12,color:t.dim}}><MDWrap>{md(previewContent)}</MDWrap></div>
                :previewContent
                ?<pre style={{margin:0,whiteSpace:"pre-wrap",fontSize:11,color:t.dim,fontFamily:font,lineHeight:1.6}}>{previewContent}</pre>
                :!["png","jpg","jpeg","gif","webp","svg","archive"].includes(previewFile.type)
                ?<div style={{display:"flex",flexDirection:"column",alignItems:"center",justifyContent:"center",height:200,color:t.mut,fontSize:12,gap:6}}><span style={{fontSize:32}}>⏳</span>Loading...</div>
                :previewFile.type==="archive"&&!previewArchive&&!previewError
                ?<div style={{display:"flex",flexDirection:"column",alignItems:"center",justifyContent:"center",height:200,color:t.mut,fontSize:12,gap:6}}><span style={{fontSize:32}}>📦</span>Reading archive...</div>
                :null
              }
            </div>
          </div>}
      </div>{/* end panels + preview wrapper */}
    </div>


    {/* KB File Preview Modal */}
    {kbPreview&&<div style={{position:"fixed",inset:0,zIndex:100,background:"rgba(0,0,0,.7)",display:"flex",alignItems:"center",justifyContent:"center",backdropFilter:"blur(6px)"}} onClick={e=>{if(e.target===e.currentTarget)setKbPreview(null);}}>
      <div style={{background:t.bgDeep,border:`1px solid ${t.brd}44`,borderRadius:16,width:"min(720px,95vw)",maxHeight:"85vh",display:"flex",flexDirection:"column",boxShadow:`0 8px 48px #0008`,overflow:"hidden",animation:"fadeIn .25s"}}>
        <div style={{padding:"12px 20px",borderBottom:`1px solid ${t.brd}22`,display:"flex",alignItems:"center",gap:8,flexShrink:0,background:`${t.surface}88`}}>
          <span style={{fontSize:14}}>{kbPreview.isPdf?"📕":"📄"}</span>
          <span style={{fontSize:13,fontWeight:700,color:t.acc,flex:1,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{kbPreview.filename}</span>
          {kbPreview.isPdf&&!kbPreview.pdfFullView&&kbPreview.truncated&&<span style={{fontSize:9,color:t.warm,flexShrink:0}}>Pages 1-{kbPreview.previewed_pages} of {kbPreview.total_pages}</span>}
          {kbPreview.truncated&&!kbPreview.isPdf&&<span style={{fontSize:9,color:t.warm,flexShrink:0}}>Showing first 200 of {kbPreview.total_lines} lines</span>}
          {kbPreview.isPdf&&kbPreview.rawUrl&&<button onClick={()=>setKbPreview(p=>({...p,pdfFullView:!p.pdfFullView}))} style={{background:kbPreview.pdfFullView?`${t.acc}25`:`${t.acc}12`,border:`1px solid ${t.acc}44`,color:t.acc,cursor:"pointer",padding:"3px 10px",borderRadius:6,fontSize:10,fontWeight:600,flexShrink:0,display:"flex",alignItems:"center",gap:4}}>{kbPreview.pdfFullView?"Text View":"Full PDF"}</button>}
          <button onClick={()=>setKbPreview(null)} style={{background:"none",border:"none",color:t.mut,cursor:"pointer",fontSize:18,padding:"0 4px",lineHeight:1}}>✕</button>
        </div>
        <div style={{flex:1,overflowY:"auto",padding:"16px 20px"}}>
          {kbPreview.loading?<div style={{textAlign:"center",padding:40,color:t.mut}}>Loading preview...</div>
          :kbPreview.isPdf&&kbPreview.pdfFullView?<iframe src={kbPreview.rawUrl} style={{width:"100%",height:"75vh",border:"none",borderRadius:8}}/>
          :kbPreview.rawUrl&&kbPreview.isImage?<img src={kbPreview.rawUrl} style={{maxWidth:"100%",maxHeight:"75vh",objectFit:"contain",borderRadius:8}} alt={kbPreview.filename}/>
          :<pre style={{fontSize:11,lineHeight:1.6,color:t.text,fontFamily:font,whiteSpace:"pre-wrap",wordBreak:"break-word",margin:0}}>{kbPreview.content}</pre>}
        </div>
      </div>
    </div>}

    {confirmDialog&&ReactDOM.createPortal((()=>{const c={danger:t.err,warning:t.warm,success:t.ok,info:t.acc}[confirmDialog.tone]||t.acc;const requiredText=confirmDialog.requiredText||"";const phraseOk=!requiredText||confirmPhrase===requiredText;const close=v=>{const dlg=confirmDialog;setConfirmDialog(null);setConfirmPhrase("");dlg.resolve&&dlg.resolve(v);};return <div style={{position:"fixed",inset:0,zIndex:500,background:"rgba(0,0,0,.66)",display:"flex",alignItems:"center",justifyContent:"center",padding:18,fontFamily:font,color:t.text}} onClick={e=>{if(e.target===e.currentTarget)close(false);}}>
      <div style={{width:"min(420px,94vw)",background:t.bgDeep,border:`1px solid ${c}44`,borderRadius:14,boxShadow:"0 18px 70px rgba(0,0,0,.55)",padding:18,animation:"fadeIn .16s"}}>
        <div style={{display:"flex",alignItems:"center",gap:10,marginBottom:10}}>
          <div style={{width:28,height:28,borderRadius:8,display:"flex",alignItems:"center",justifyContent:"center",background:`${c}16`,border:`1px solid ${c}35`,color:c,fontWeight:800}}>!</div>
          <div style={{fontSize:14,fontWeight:800,color:t.text,letterSpacing:.3}}>{confirmDialog.title}</div>
        </div>
        <div style={{fontSize:12,color:t.dim,lineHeight:1.6,marginBottom:18}}>{confirmDialog.body}</div>
        {requiredText&&<div style={{margin:"-4px 0 18px",display:"grid",gap:7}}>
          <div style={{fontSize:10,color:t.mut,textTransform:"uppercase",letterSpacing:.6,fontWeight:800}}>{confirmDialog.inputLabel}</div>
          <input value={confirmPhrase} onChange={e=>setConfirmPhrase(e.target.value)} placeholder={requiredText} autoFocus style={{...inputS,borderColor:phraseOk?`${c}66`:`${c}33`,background:`${c}08`,fontSize:12}}/>
        </div>}
        <div style={{display:"flex",justifyContent:"flex-end",gap:8}}>
          <button onClick={()=>close(false)} style={{...btnS(t.mut),fontSize:12,padding:"7px 12px"}}>{confirmDialog.cancelLabel}</button>
          <button onClick={()=>phraseOk&&close(true)} disabled={!phraseOk} style={{...btnS(c),fontSize:12,padding:"7px 13px",fontWeight:800,opacity:phraseOk?1:.42,cursor:phraseOk?"pointer":"not-allowed"}}>{confirmDialog.confirmLabel}</button>
        </div>
      </div>
    </div>;})(),document.body)}

    {/* Toast host */}
    {toasts.length>0&&<div style={{position:"fixed",bottom:20,left:"50%",transform:"translateX(-50%)",zIndex:520,display:"flex",flexDirection:"column",gap:7,pointerEvents:"none",width:"min(440px,calc(100vw - 28px))"}}>
      {toasts.map(tt=>{const c={success:t.ok,error:t.err,warning:t.warm,info:t.acc}[tt.type||"info"]||t.acc;const action=tt.action||(tt.onUndo?{label:"Undo",onClick:tt.onUndo}:null);return <div key={tt.id} style={{display:"flex",alignItems:"center",gap:10,background:t.bgDeep,border:`1px solid ${c}55`,borderRadius:10,padding:"9px 12px",fontSize:12,color:t.dim,boxShadow:`0 8px 24px rgba(0,0,0,.4)`,pointerEvents:"auto",animation:"fadeIn .2s"}}>
        <span style={{width:8,height:8,borderRadius:"50%",background:c,boxShadow:`0 0 8px ${c}88`,flexShrink:0}}/>
        <span style={{display:"flex",flexDirection:"column",gap:2,minWidth:0,flex:1}}>
          <span style={{color:t.text,fontWeight:700,whiteSpace:"nowrap",overflow:"hidden",textOverflow:"ellipsis"}}>{tt.text}</span>
          {tt.detail&&<span style={{fontSize:10,color:t.mut,lineHeight:1.35,wordBreak:"break-word"}}>{tt.detail}</span>}
        </span>
        {action&&<button onClick={()=>{action.onClick&&action.onClick();setToasts(ts=>ts.filter(x=>x.id!==tt.id));}} style={{background:"none",border:`1px solid ${c}55`,color:c,cursor:"pointer",padding:"3px 9px",borderRadius:6,fontFamily:font,fontSize:11,fontWeight:700,flexShrink:0}}>{action.label}</button>}
        <button onClick={()=>setToasts(ts=>ts.filter(x=>x.id!==tt.id))} style={{background:"none",border:"none",color:t.mut,cursor:"pointer",padding:"1px 3px",fontSize:13,flexShrink:0}}>×</button>
      </div>;})}
    </div>}

    <style>{`
      @import url('https://fonts.googleapis.com/css2?family=${FONTS[fi].i}&display=swap');
      @keyframes blink{0%,50%{opacity:1}51%,100%{opacity:0}}
      @keyframes pulse{0%,80%,100%{opacity:.3;transform:scale(.8)}40%{opacity:1;transform:scale(1.2)}}
      @keyframes slide{0%{transform:translateX(-100%)}100%{transform:translateX(350%)}}
      .composer-trace{position:absolute;display:block;background:linear-gradient(90deg,transparent,var(--trace-color),transparent);filter:drop-shadow(0 0 4px var(--trace-color));opacity:.52;pointer-events:none}
      .composer-trace-top{top:0;left:0;width:42%;height:2px;animation:traceX 1.25s linear infinite}
      .composer-trace-bottom{right:0;bottom:0;width:42%;height:2px;animation:traceXReverse 1.25s linear infinite}
      .composer-trace-right{top:0;right:0;width:2px;height:50%;background:linear-gradient(180deg,transparent,var(--trace-color),transparent);animation:traceY 1.25s linear infinite}
      .composer-trace-left{left:0;bottom:0;width:2px;height:50%;background:linear-gradient(180deg,transparent,var(--trace-color),transparent);animation:traceYReverse 1.25s linear infinite}
      .empty-composer-box{animation:emptyComposerAura .75s ease-out .08s both}
      @keyframes emptyComposerAura{0%{box-shadow:0 0 0 rgba(0,0,0,0)}42%{box-shadow:0 0 0 1px var(--empty-composer-glow),0 0 34px var(--empty-composer-glow)}100%{box-shadow:0 0 0 rgba(0,0,0,0)}}
      @keyframes traceX{0%{transform:translateX(-115%)}100%{transform:translateX(260%)}}
      @keyframes traceXReverse{0%{transform:translateX(115%)}100%{transform:translateX(-260%)}}
      @keyframes traceY{0%{transform:translateY(-115%)}100%{transform:translateY(260%)}}
      @keyframes traceYReverse{0%{transform:translateY(115%)}100%{transform:translateY(-260%)}}
      @keyframes marquee-think{0%{transform:translate3d(0,0,0)}100%{transform:translate3d(-50%,0,0)}}
      .pill-marquee-wrap{overflow:hidden;max-width:280px;display:inline-block;vertical-align:middle;-webkit-mask-image:linear-gradient(to right,transparent 0%,black 8%,black 92%,transparent 100%);mask-image:linear-gradient(to right,transparent 0%,black 8%,black 92%,transparent 100%)}
      .pill-marquee-inner{display:inline-block;white-space:nowrap;animation:marquee-think 12s linear infinite;will-change:transform;backface-visibility:hidden}
      @keyframes fadeIn{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:translateY(0)}}
      @keyframes glow{0%,100%{box-shadow:0 0 8px ${t.acc}88}50%{box-shadow:0 0 16px ${t.acc}CC}}
      @keyframes pGlow{0%,100%{box-shadow:0 0 4px ${t.err}44}50%{box-shadow:0 0 14px ${t.err}88}}
      @keyframes pCouncilGlow{0%,100%{box-shadow:0 0 4px ${t.pink}44}50%{box-shadow:0 0 14px ${t.pink}88}}
      @keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-5px)}}
      @keyframes spin{0%{transform:rotate(0deg)}100%{transform:rotate(360deg)}}
      @keyframes bounce{0%,100%{transform:translateY(0)}50%{transform:translateY(-4px)}}
      @keyframes swing{0%,100%{transform:rotate(-12deg)}50%{transform:rotate(12deg)}}
      @keyframes bop{0%,100%{transform:scale(1)}50%{transform:scale(1.3)}}
      @keyframes memoryPulse{0%{transform:scale(1);opacity:1}25%{transform:scale(1.3);opacity:.7}50%{transform:scale(1);opacity:1}75%{transform:scale(1.2);opacity:.8}100%{transform:scale(1);opacity:1}}
      @keyframes writing{0%{transform:rotate(-8deg) translateY(0)}25%{transform:rotate(4deg) translateY(-1px)}50%{transform:rotate(-6deg) translateY(0)}75%{transform:rotate(5deg) translateY(-1px)}100%{transform:rotate(-8deg) translateY(0)}}
      @keyframes pillGlow {
  0%,100%{box-shadow:0 0 4px var(--pill-glow,rgba(120,200,255,.25));}
  50%{box-shadow:0 0 10px var(--pill-glow,rgba(120,200,255,.55));}
}
	      @keyframes glitch{0%,90%,100%{transform:skewX(0)}92%{transform:skewX(-5deg)}94%{transform:skewX(5deg)}96%{transform:skewX(-3deg)}98%{transform:skewX(3deg)}}
	      @keyframes scanline{0%{transform:translateY(-100%)}100%{transform:translateY(100vh)}}
	      @keyframes ripple{0%{transform:scale(0);opacity:1}100%{transform:scale(4);opacity:0}}
	      @keyframes convTitleMarquee{0%,12%{transform:translateX(0)}88%,100%{transform:translateX(calc(-1 * var(--conv-title-marquee,0px)))}}
	      a[download]:hover{background:${t.ok}28 !important;border-color:${t.ok}66 !important;transform:translateY(-1px);box-shadow:0 3px 12px ${t.ok}22;}
      a[download]:active{transform:translateY(0);}
      .nav-panel-button:hover{background:var(--nav-hover-bg) !important;border-color:var(--nav-hover-border) !important;color:var(--nav-hover-color) !important;box-shadow:0 0 0 1px var(--nav-hover-ring),0 4px 14px var(--nav-hover-shadow) !important;transform:translateY(-1px);}
      .nav-panel-button.is-active:hover{background:var(--nav-active-hover-bg) !important;}
      .nav-panel-button:active{transform:translateY(0);}
	      .conv-row{transition:background .14s ease,border-color .14s ease;}
	      .conv-row:not(.is-act):hover{background:var(--conv-row-hover-bg) !important;--conv-action-bg:var(--conv-row-hover-bg);}
	      .conv-title-clip{display:block;-webkit-mask-image:linear-gradient(90deg,#000 0%,#000 calc(100% - 34px),rgba(0,0,0,.86) calc(100% - 22px),rgba(0,0,0,.35) calc(100% - 9px),transparent 100%);mask-image:linear-gradient(90deg,#000 0%,#000 calc(100% - 34px),rgba(0,0,0,.86) calc(100% - 22px),rgba(0,0,0,.35) calc(100% - 9px),transparent 100%);}
	      .conv-title-text{display:inline-block;white-space:nowrap;padding-right:24px;will-change:transform;}
	      .conv-row[data-title-overflow="1"]:hover .conv-title-text,.conv-row[data-title-overflow="1"]:focus-within .conv-title-text{animation:convTitleMarquee 9s .35s linear infinite alternate;}
	      @media (prefers-reduced-motion:reduce){.conv-row[data-title-overflow="1"]:hover .conv-title-text,.conv-row[data-title-overflow="1"]:focus-within .conv-title-text{animation:none;}}
	      .conv-actions{z-index:2;}
	      .conv-actions::before{content:"";position:absolute;inset:-7px -6px -7px -4px;z-index:0;pointer-events:none;background:linear-gradient(90deg,transparent 0%,var(--conv-action-bg) 18%,var(--conv-action-bg) 100%),linear-gradient(90deg,transparent 0%,${t.bgDeep}E6 16%,${t.bgDeep}E6 100%);-webkit-backdrop-filter:blur(7px) saturate(1.08);backdrop-filter:blur(7px) saturate(1.08);opacity:0;transition:opacity .14s ease,background .14s ease;}
	      .conv-row:hover .conv-actions::before,.conv-row:focus-within .conv-actions::before,.conv-actions.has-sticky-actions::before{opacity:.94;}
	      .conv-act{opacity:0;transition:opacity .14s ease,color .14s ease,background .14s ease;}
      .conv-act{position:relative;z-index:1;}
      .conv-act.on{opacity:1;}
      .conv-row:hover .conv-act{opacity:1;}
      .conv-act:hover{background:${t.sfBri}33 !important;}
      .conv-del:hover{color:${t.err} !important;}
      *{scrollbar-width:auto;scrollbar-color:${t.sfBri} transparent}
      *::-webkit-scrollbar{width:7px;height:10px}*::-webkit-scrollbar-track{background:transparent;border-radius:5px}
      *::-webkit-scrollbar-thumb{background:${t.sfBri};border-radius:5px}*::-webkit-scrollbar-thumb:hover{background:${t.acc}88}
      ::selection{background:${t.acc}44}
      textarea::placeholder,input::placeholder{color:${t.mut}55}
      select option{background:${t.bgDeep};color:${t.text}}
      button{transition:filter .15s ease,border-color .15s ease,background-color .15s ease,color .15s ease;position:relative;overflow:hidden}button:hover{filter:brightness(1.08)}button:active{filter:brightness(.98)}
    `}</style>
  </div>;
}

ReactDOM.createRoot(document.getElementById("root")).render(<HyprChat/>);
