// Is a file text the model can read? `File.text()` never fails: a zip or an image read that way becomes
// thousands of replacement characters, which were sent to the model as the file's "content".
export function looksBinary(bytes) {
  const sample = bytes || [];
  if (!sample.length) return false;
  let odd = 0;
  for (const byte of sample) {
    if (byte === 0) return true;
    if (byte < 32 && ![9, 10, 12, 13, 27].includes(byte)) odd += 1;
  }
  return odd / sample.length > 0.1;
}
