import React, {useEffect, useState} from 'react';

// A number box that saves when you leave it or press Enter, not on every keystroke.
//
// The context-size box used to save each keystroke: typing 16384 over 8192 saved 81, 816, 8163 on the
// way (each one applied to chats sent meanwhile), and the last digit could not be deleted because an
// empty box was refused. Here the box holds a draft; `valid(n)` decides whether it may be saved, an
// invalid or empty draft snaps back to the saved value, and Escape discards the draft.
export default function CommitNumberInput({value, onCommit, valid = n => n > 0, ...rest}) {
  const [draft, setDraft] = useState(String(value ?? ''));
  useEffect(() => { setDraft(String(value ?? '')); }, [value]);
  const commit = () => {
    const n = Number(draft);
    if (draft.trim() !== '' && Number.isFinite(n) && valid(n)) { if (n !== value) onCommit(n); else setDraft(String(value ?? '')); }
    else setDraft(String(value ?? ''));
  };
  return <input {...rest} type="number" value={draft} onChange={e => setDraft(e.target.value)} onBlur={commit}
    onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); commit(); e.currentTarget.blur(); } if (e.key === 'Escape') { setDraft(String(value ?? '')); e.currentTarget.blur(); } }}/>;
}
