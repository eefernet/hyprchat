// Shared production grammar registry; tests exercise these same imports.
// prism-setup must come before the component grammars (see its comment).
import Prism from './prism-setup.js';
import 'prismjs/components/prism-markup.js';      // html / xml / svg
import 'prismjs/components/prism-css.js';
import 'prismjs/components/prism-clike.js';
import 'prismjs/components/prism-javascript.js';
import 'prismjs/components/prism-jsx.js';
import 'prismjs/components/prism-typescript.js';
import 'prismjs/components/prism-tsx.js';
import 'prismjs/components/prism-json.js';
import 'prismjs/components/prism-python.js';
import 'prismjs/components/prism-bash.js';
import 'prismjs/components/prism-yaml.js';
import 'prismjs/components/prism-markdown.js';
import 'prismjs/components/prism-sql.js';
import 'prismjs/components/prism-go.js';
import 'prismjs/components/prism-rust.js';
import 'prismjs/components/prism-swift.js';
import 'prismjs/components/prism-c.js';
import 'prismjs/components/prism-cpp.js';
import 'prismjs/components/prism-java.js';
import 'prismjs/components/prism-csharp.js';
import 'prismjs/components/prism-ruby.js';
import 'prismjs/components/prism-markup-templating.js'; // required by PHP's global hooks
import 'prismjs/components/prism-php.js';
import 'prismjs/components/prism-toml.js';
import 'prismjs/components/prism-docker.js';
import 'prismjs/components/prism-diff.js';
import 'prismjs/components/prism-ini.js';

export default Prism;
