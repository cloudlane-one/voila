import { IOutput } from '@jupyterlab/nbformat';
import { OutputAreaModel, SimplifiedOutputArea } from '@jupyterlab/outputarea';
import { IRenderMimeRegistry } from '@jupyterlab/rendermime';
import { Widget } from '@lumino/widgets';
import { PageConfig, URLExt } from '@jupyterlab/coreutils';
import { type VoilaWidgetManager } from '@voila-dashboards/widgets-manager8/lib/manager';

/**
 * Interface representing the structure of an execution result message.
 */
export interface IExecutionResultMessage {
  action: 'execution_result';
  payload: {
    output_cell: { outputs: IOutput[] };
    cell_index: number;
    total_cell: number;
  };
}

/**
 * Interface representing the structure of an execution error message.
 */
export interface IExecutionErrorMessage {
  action: 'execution_error';
  payload: {
    error: string;
  };
}

/**
 * A `clear_output` message, streamed along with the cell outputs since it
 * changes how they are displayed.
 */
export interface IClearOutput {
  output_type: 'clear_output';
  wait?: boolean;
}

/**
 * An output streamed from a cell which is still executing.
 *
 * `update_display_data` messages are streamed with their `transient` field,
 * which identifies the outputs they update.
 */
export type IStreamedOutput = IOutput | IClearOutput;

/**
 * Interface representing the structure of a streamed output message, sent
 * for every output of a cell while it is still executing.
 */
export interface IExecutionOutputMessage {
  action: 'execution_output';
  payload: {
    cell_index: number;
    output: IStreamedOutput;
  };
}

/**
 * Interface representing a received widget model
 * containing output and execution models.
 */
export interface IReceivedWidgetModel {
  [modelId: string]: {
    outputModel: OutputAreaModel;
    executionModel: IOutput;
  };
}
export type IExecutionMessage =
  | IExecutionResultMessage
  | IExecutionErrorMessage
  | IExecutionOutputMessage;

export function getExecutionURL(kernelId?: string): string {
  const wsUrl = PageConfig.getWsUrl();
  return URLExt.join(wsUrl, 'voila/execution', kernelId ?? '');
}

/**
 * Handles the execution result by rendering the output area and managing widget models.
 *
 * @param payload - The payload from the execution result message,
 *  including the output cell and cell index.
 * @param rendermime - A render mime registry to render the output.
 * @param widgetManager - The Voila widget manager to manage Jupyter widgets.
 * @returns An object containing a model ID and its corresponding output and
 * execution models if the model is not ready to be rendered, undefined otherwise.
 */
export function handleExecutionResult({
  payload,
  rendermime,
  widgetManager
}: {
  payload: IExecutionResultMessage['payload'];
  rendermime: IRenderMimeRegistry;
  widgetManager: VoilaWidgetManager;
}): IReceivedWidgetModel {
  const { cell_index, output_cell } = payload;
  const element = document.querySelector(`[cell-index="${cell_index + 1}"]`);
  let receivedWidgetModel: IReceivedWidgetModel = {};

  if (element) {
    const skeleton = element
      .getElementsByClassName('voila-skeleton-container')
      .item(0);
    if (skeleton) {
      element.removeChild(skeleton);
    }
    const [model] = createOutputArea({ rendermime, parent: element });
    if (!output_cell.outputs) {
      return receivedWidgetModel;
    }
    if (output_cell.outputs.length > 0) {
      element.lastElementChild?.classList.remove(
        'jp-mod-noOutputs',
        'jp-mod-noInput'
      );
    }
    const key = 'application/vnd.jupyter.widget-view+json';
    for (const outputData of output_cell.outputs) {
      const modelId = (outputData?.data as any)?.[key]?.model_id;
      if (modelId) {
        if (widgetManager.registeredModels.has(modelId)) {
          model.add(outputData);
        } else {
          receivedWidgetModel = {
            ...receivedWidgetModel,
            [modelId]: {
              outputModel: model,
              executionModel: outputData
            }
          };
        }
      } else {
        model.add(outputData);
      }
    }
  }
  return receivedWidgetModel;
}

/**
 * Creates an output area model and attaches the output area to a specified parent element.
 *
 * @param rendermime - The render mime registry.
 * @param parent - The parent HTML element where the output area will be appended.
 * @returns The created OutputAreaModel.
 */
export function createOutputArea({
  rendermime,
  parent
}: {
  rendermime: IRenderMimeRegistry;
  parent: Element;
}): [OutputAreaModel, SimplifiedOutputArea] {
  const model = new OutputAreaModel({ trusted: true });
  const area = new SimplifiedOutputArea({
    model,
    rendermime
  });

  const wrapper = document.createElement('div');
  wrapper.classList.add('jp-Cell-outputWrapper');
  const collapser = document.createElement('div');
  collapser.classList.add(
    'jp-Collapser',
    'jp-OutputCollapser',
    'jp-Cell-outputCollapser'
  );
  wrapper.appendChild(collapser);
  parent.lastElementChild?.appendChild(wrapper);
  area.node.classList.add('jp-Cell-outputArea');

  area.node.style.display = 'flex';
  area.node.style.flexDirection = 'column';

  Widget.attach(area, wrapper);
  return [model, area];
}

/**
 * The output area displaying the outputs of a cell which is still executing.
 *
 * It is used to display the outputs as the kernel produces them, and is
 * replaced by the rendering of the final cell outputs once the cell is done
 * executing.
 */
export interface ILiveOutputArea {
  model: OutputAreaModel;
  area: SimplifiedOutputArea;
  /**
   * Indexes of the outputs displayed under each display id, used to update
   * the outputs an `update_display_data` message refers to.
   */
  displayIds: Map<string, number[]>;
  dispose(): void;
}

/**
 * Creates the output area displaying the outputs of a still executing cell.
 *
 * @param rendermime - The render mime registry.
 * @param parent - The parent HTML element where the output area will be appended.
 * @returns The created live output area.
 */
export function createLiveOutputArea({
  rendermime,
  parent
}: {
  rendermime: IRenderMimeRegistry;
  parent: Element;
}): ILiveOutputArea {
  const [model, area] = createOutputArea({ rendermime, parent });
  const wrapper = area.node.parentElement;
  return {
    model,
    area,
    displayIds: new Map<string, number[]>(),
    dispose: (): void => {
      Widget.detach(area);
      area.dispose();
      model.dispose();
      wrapper?.remove();
    }
  };
}

/**
 * Displays an output streamed from a still executing cell.
 *
 * This mirrors the way JupyterLab handles the IOPub messages of a cell it is
 * executing: contiguous stream outputs of the same name are combined,
 * `clear_output` messages clear the displayed outputs (possibly after the
 * next output, when `wait` is set) and `update_display_data` messages update
 * the outputs previously displayed under the same display id.
 *
 * @param liveOutputArea - The output area of the executing cell.
 * @param output - The streamed output.
 */
export function addLiveOutput(
  liveOutputArea: ILiveOutputArea,
  output: IStreamedOutput
): void {
  const { model, displayIds } = liveOutputArea;
  // `transient` is not part of the `IOutput` type, but it is what identifies
  // the outputs an `update_display_data` message refers to.
  const displayId: string | undefined = (output as any).transient?.display_id;

  switch (output.output_type) {
    case 'execute_result':
    case 'display_data':
    case 'stream':
    case 'error':
      model.add(output as IOutput);
      if (displayId && output.output_type === 'display_data') {
        const indexes = displayIds.get(displayId) ?? [];
        indexes.push(model.length - 1);
        displayIds.set(displayId, indexes);
      }
      break;
    case 'clear_output':
      displayIds.clear();
      model.clear((output as IClearOutput).wait ?? false);
      break;
    case 'update_display_data': {
      const indexes = displayId ? displayIds.get(displayId) : undefined;
      if (indexes) {
        const displayData = {
          ...output,
          output_type: 'display_data'
        } as IOutput;
        for (const index of indexes) {
          model.set(index, displayData);
        }
      }
      break;
    }
    default:
      break;
  }
}

/**
 * Creates an output area model and attaches the output area to a specified parent element.
 *
 * @param model - The output model to be rendered.
 * @param rendermime - The render mime registry.
 * @param parent - The parent HTML element where the output will be appended.
 * @returns The created OutputAreaModel.
 */
export function createReactiveOutput({
  model,
  rendermime,
  parent
}: {
  model: any;
  rendermime: IRenderMimeRegistry;
  parent: HTMLElement;
}): Widget | null {
  const mimeType = rendermime.preferredMimeType(model.data, 'any');

  if (!mimeType) {
    return null;
  }
  const output = rendermime.createRenderer(mimeType);
  output.renderModel(model).catch((error) => {
    // Manually append error message to output
    const pre = document.createElement('pre');
    pre.textContent = `Javascript Error: ${error.message}`;
    output.node.appendChild(pre);

    // Remove mime-type-specific CSS classes
    pre.className = 'lm-Widget jp-RenderedText';
    pre.setAttribute('data-mime-type', 'application/vnd.jupyter.stderr');
  });

  output.addClass('jp-OutputArea-output');

  // Attach output
  Widget.attach(output, parent);

  return output;
}

export function createSkeleton(): void {
  const innerHtml = `<div class="voila-skeleton-post">
      <div class="voila-skeleton-avatar"></div>
      <div class="voila-skeleton-line"></div>
      <div class="voila-skeleton-line"></div>
    </div>`;
  const elements = document.querySelectorAll('[cell-index]');
  elements.forEach((it) => {
    const element = document.createElement('div');
    element.className = 'voila-skeleton-container';
    element.innerHTML = innerHtml;
    it.appendChild(element);
  });
}
