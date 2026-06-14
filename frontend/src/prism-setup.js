// Prism core + global registration.
//
// Prism's language component modules (prismjs/components/prism-*) register
// themselves against a Prism instance they find on the global object at the
// moment they evaluate. ES module imports run in source order, each fully
// before the next, so importing THIS module before the components guarantees
// window.Prism is set first. (Assigning window.Prism inline between import
// statements would NOT work — all imports in a module are hoisted/evaluated
// ahead of the module body.)
import Prism from 'prismjs';

if (typeof window !== 'undefined') {
  window.Prism = Prism;
}

export default Prism;
