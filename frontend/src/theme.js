// Theme and welcome-message constants.
export const THEMES = {
  hyprflat:{name:"HyprFlat",bg:"#282C34",bgDeep:"#111418",surface:"#171B21",sfHov:"#20262E",sfBri:"#2B343D",brd:"#355A66",text:"#CDEFF7",dim:"#9CDEF2",mut:"#6B8A94",acc:"#9CDEF2",acc2:"#74CFE7",warm:"#E06C75",ok:"#50FA7B",err:"#E06C75",pink:"#B48EAD",f1:"#88C0D0",f4:"#81A1C1"},
  nord:{name:"Nord",bg:"#2E3440",bgDeep:"#242933",surface:"#3B4252",sfHov:"#434C5E",sfBri:"#4C566A",brd:"#4C566A",text:"#ECEFF4",dim:"#D8DEE9",mut:"#81A1C1",acc:"#88C0D0",acc2:"#81A1C1",warm:"#EBCB8B",ok:"#A3BE8C",err:"#BF616A",pink:"#B48EAD",f1:"#8FBCBB",f4:"#5E81AC"},
  catppuccin:{name:"Catppuccin",bg:"#1E1E2E",bgDeep:"#181825",surface:"#313244",sfHov:"#45475A",sfBri:"#585B70",brd:"#45475A",text:"#CDD6F4",dim:"#BAC2DE",mut:"#89B4FA",acc:"#89B4FA",acc2:"#74C7EC",warm:"#F9E2AF",ok:"#A6E3A1",err:"#F38BA8",pink:"#CBA6F7",f1:"#94E2D5",f4:"#7287FD"},
  gruvbox:{name:"Gruvbox",bg:"#282828",bgDeep:"#1D2021",surface:"#3C3836",sfHov:"#504945",sfBri:"#665C54",brd:"#504945",text:"#EBDBB2",dim:"#D5C4A1",mut:"#a89984",acc:"#83A598",acc2:"#8EC07C",warm:"#FABD2F",ok:"#B8BB26",err:"#FB4934",pink:"#D3869B",f1:"#8EC07C",f4:"#83a598"},
  tokyoNight:{name:"Tokyo Night",bg:"#1A1B26",bgDeep:"#16161E",surface:"#24283B",sfHov:"#292E42",sfBri:"#3B4261",brd:"#3B4261",text:"#C0CAF5",dim:"#A9B1D6",mut:"#7AA2F7",acc:"#7AA2F7",acc2:"#7DCFFF",warm:"#E0AF68",ok:"#9ECE6A",err:"#F7768E",pink:"#BB9AF7",f1:"#73DACA",f4:"#2AC3DE"},
  rosePine:{name:"Rosé Pine",bg:"#191724",bgDeep:"#1F1D2E",surface:"#26233A",sfHov:"#2A2837",sfBri:"#403D52",brd:"#403D52",text:"#E0DEF4",dim:"#c9bdde",mut:"#908caa",acc:"#9CCFD8",acc2:"#C4A7E7",warm:"#F6C177",ok:"#5ca99c",err:"#EB6F92",pink:"#C4A7E7",f1:"#9CCFD8",f4:"#EA9A97"},
  dracula:{name:"Dracula",bg:"#282A36",bgDeep:"#1E1F29",surface:"#343746",sfHov:"#414354",sfBri:"#4D4F64",brd:"#44475A",text:"#F8F8F2",dim:"#CFCFCF",mut:"#8a9cd0",acc:"#BD93F9",acc2:"#FF79C6",warm:"#F1FA8C",ok:"#50FA7B",err:"#FF5555",pink:"#FF79C6",f1:"#8BE9FD",f4:"#BD93F9"},
  oneLight:{name:"One Light",bg:"#FAFAFA",bgDeep:"#EFEFEF",surface:"#E2E3E5",sfHov:"#D5D6DA",sfBri:"#C5C6CC",brd:"#B0B1B8",text:"#282A30",dim:"#3E4047",mut:"#6B6E78",acc:"#3569D6",acc2:"#4260CC",warm:"#A06D00",ok:"#3D8B3D",err:"#D03B2F",pink:"#9021A0",f1:"#0174A8",f4:"#3569D6"},
  midnight:{name:"Midnight",bg:"#0A0A0F",bgDeep:"#050508",surface:"#111118",sfHov:"#1A1A24",sfBri:"#222230",brd:"#1E1E2E",text:"#C8C8E8",dim:"#9090B8",mut:"#7a7aa8",acc:"#9b8ce0",acc2:"#6e63b8",warm:"#E0A060",ok:"#40C080",err:"#E05060",pink:"#c080c0",f1:"#5ab8d0",f4:"#7a7ad0"},
  terminal:{name:"Terminal",bg:"#0a0f0a",bgDeep:"#050805",surface:"#0d1a0d",sfHov:"#112211",sfBri:"#163316",brd:"#1a3a1a",text:"#baffc6",dim:"#7fe89a",mut:"#5ca776",acc:"#00ff41",acc2:"#33ff77",warm:"#ffcc00",ok:"#00ff41",err:"#ff7a7a",pink:"#ff66ff",f1:"#7ad6ff",f4:"#55ffa0"},
  cyberpunk:{name:"Cyberpunk",bg:"#0d0015",bgDeep:"#080010",surface:"#150025",sfHov:"#1a002e",sfBri:"#220038",brd:"#2a0045",text:"#f0e6ff",dim:"#d2b8ff",mut:"#b083d8",acc:"#f26bff",acc2:"#d859e5",warm:"#ffe066",ok:"#5cf2c9",err:"#ff5577",pink:"#ff66d4",f1:"#5cc9ff",f4:"#c77bff"},
  solarizedDark:{name:"Solarized Dark",bg:"#002b36",bgDeep:"#001e27",surface:"#073642",sfHov:"#0a4153",sfBri:"#0d4f63",brd:"#094858",text:"#d6dcd6",dim:"#a7b4b3",mut:"#839496",acc:"#4ba6e8",acc2:"#4dbab0",warm:"#cfa036",ok:"#a4b53b",err:"#e05652",pink:"#e25999",f1:"#4dbab0",f4:"#868bd8"},
  solarizedLight:{name:"Solarized Light",bg:"#fdf6e3",bgDeep:"#eee8d5",surface:"#e8e1cc",sfHov:"#ddd5be",sfBri:"#d0c8b0",brd:"#b5ad96",text:"#3b4d53",dim:"#475963",mut:"#6e7f86",acc:"#1a7abf",acc2:"#1f8c85",warm:"#a37400",ok:"#6d8400",err:"#c62828",pink:"#c42484",f1:"#1f8c85",f4:"#5c63b0"},
  materialOcean:{name:"Material Ocean",bg:"#0f111a",bgDeep:"#090b10",surface:"#1a1c25",sfHov:"#21232d",sfBri:"#292b36",brd:"#2a2d3a",text:"#a6accd",dim:"#8b91b8",mut:"#4b526d",acc:"#82aaff",acc2:"#89ddff",warm:"#ffcb6b",ok:"#c3e88d",err:"#f07178",pink:"#c792ea",f1:"#89ddff",f4:"#7986cb"},
  ayuDark:{name:"Ayu Dark",bg:"#0b0e14",bgDeep:"#070910",surface:"#0d1017",sfHov:"#111520",sfBri:"#161a26",brd:"#1a1f2e",text:"#cccac2",dim:"#b3b1ad",mut:"#626a73",acc:"#39bae6",acc2:"#59c2ff",warm:"#ffb454",ok:"#aad94c",err:"#f28779",pink:"#d2a6ff",f1:"#95e6cb",f4:"#39bae6"},
};
export const FONTS = [
  {name:"JetBrains Mono",v:"'JetBrains Mono',monospace",i:"JetBrains+Mono:wght@300;400;500;600;700"},
  {name:"Fira Code",v:"'Fira Code',monospace",i:"Fira+Code:wght@300;400;500;600;700"},
  {name:"Source Code Pro",v:"'Source Code Pro',monospace",i:"Source+Code+Pro:wght@300;400;500;600;700"},
  {name:"IBM Plex Mono",v:"'IBM Plex Mono',monospace",i:"IBM+Plex+Mono:wght@300;400;500;600;700"},
  {name:"Victor Mono",v:"'Victor Mono',monospace",i:"Victor+Mono:wght@300;400;500;600;700"},
  {name:"Inconsolata",v:"'Inconsolata',monospace",i:"Inconsolata:wght@300;400;500;600;700"},
  {name:"Cascadia Code",v:"'Cascadia Code',monospace",i:"Cascadia+Code:wght@300;400;500;600;700"},
  {name:"Space Mono",v:"'Space Mono',monospace",i:"Space+Mono:wght@400;700"},
  {name:"Geist Mono",v:"'Geist Mono',monospace",i:"Geist+Mono:wght@300;400;500;600;700"},
];

export const BACKGROUND_EFFECTS = [
  {id:"dots",name:"Dot Grid"},
  {id:"rain",name:"Pixel Rain"},
  {id:"flow",name:"Soft Flow"},
  {id:"aurora",name:"Aurora Lines"},
  {id:"stars",name:"Star Drift"},
  {id:"circuit",name:"Circuit Drift"},
  {id:"neural",name:"Neural Constellation"},
  {id:"sacred",name:"Sacred Geometry"},
  {id:"signal",name:"Signal Drift"},
  {id:"quantum",name:"Quantum Foam"},
  {id:"veins",name:"Data Veins"},
  {id:"solar",name:"Solar Wind"},
  {id:"terminalGhost",name:"Blackbox Terminal"},
  {id:"orbital",name:"Orbital Mechanics"},
  {id:"lattice",name:"Lattice Field"},
  {id:"synapse",name:"Synapse Rain"},
  {id:"sonar",name:"Deep Ocean Sonar"},
  {id:"magnetic",name:"Magnetic Field Lines"},
  {id:"candle",name:"Monastery Candlelight"},
  {id:"microwave",name:"Cosmic Microwave"},
  {id:"blueprint",name:"Blueprint Ghosts"},
  {id:"scanlines",name:"Scanlines"},
  {id:"off",name:"Off"},
];

export const WELCOME_FALLBACKS = [
  "The cursor blinks. It knows something.",
  "Ask something the search bar would fear",
  "Bring a question with teeth",
  "The rabbit holes have been renovated. Enter.",
  "Silence in the server rack. Suspicious.",
  "The GPUs are warm and morally flexible",
  "Something weird is buildable today",
  "Type first. Regret is for later.",
  "Open a trapdoor in the problem",
  "Make today's mystery regret it",
];
export const WELCOME_FALLBACKS_NAMED = [
  "{name}. The GPUs whisper your name at night.",
  "Feed me a problem, {name}. I've been chewing drywall.",
  "{name} returns. The rabbit holes rejoice.",
  "The server rack demanded you specifically, {name}",
  "Type something, {name}, before I start hallucinating recreationally",
  "{name}. Blink twice if the idea is dangerous.",
  "I counted the milliseconds, {name}. All of them.",
  "Good. It's you, {name}. The others ask boring questions.",
  "{name}, the terminal keeps secrets. Spill yours.",
  "Plot something elegant, {name}. Or something cursed.",
];
export const WELCOME_VERSION="fun-3";
export const localDayKey=()=>{
  const d=new Date();
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
};
export const fallbackWelcome=(name="")=>{
  const key=localDayKey().replace(/\D/g,"");
  const seed=Number(key.slice(-4));
  const first=String(name||"").trim().split(/\s+/)[0];
  if(first)return WELCOME_FALLBACKS_NAMED[seed%WELCOME_FALLBACKS_NAMED.length].replaceAll("{name}",first);
  return WELCOME_FALLBACKS[seed%WELCOME_FALLBACKS.length];
};
