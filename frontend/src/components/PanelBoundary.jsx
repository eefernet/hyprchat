import React from 'react';

export default class PanelBoundary extends React.Component {
  state={failed:false};
  static getDerivedStateFromError(){return {failed:true};}
  render(){
    if(this.state.failed)return <section role="alert" style={{padding:24,flex:1}}><p>This panel could not be displayed. Your saved work is retained.</p><button onClick={()=>this.setState({failed:false})}>Try displaying it again</button></section>;
    return this.props.children;
  }
}
