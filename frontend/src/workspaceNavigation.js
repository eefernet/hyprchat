export const workspaceSections=[['overview','Overview'],['convs','Chats'],['knowledge','Sources'],['notes','Notes'],['tasks','Activity'],['settings','Settings']];
export function workspaceSection(tab){return ({reports:'knowledge',files:'knowledge',jobs:'tasks',memory:'settings'})[tab]||tab;}
export function workspaceSubviews(tab){return ({knowledge:[['knowledge','All sources'],['reports','Reports'],['files','Artifacts']],tasks:[['tasks','Scheduled Tasks'],['jobs','Jobs']],settings:[['settings','Chat defaults'],['memory','Memory']]})[workspaceSection(tab)]||[];}
export function workspaceSectionLabel(tab){return workspaceSections.find(([id])=>id===workspaceSection(tab))?.[1]||'Overview';}
