declare module "plotly.js-dist-min" {
  const Plotly: {
    react: (host: HTMLElement, data: any[], layout: any, config: any) => Promise<any>;
    purge: (host: HTMLElement) => void;
    Plots: { resize: (host: HTMLElement) => Promise<any> };
  };
  export default Plotly;
}
