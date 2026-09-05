# ⚠️ 部署 Genie TTS 服务

[← 文档索引](README.md) · [项目主页](../README.md)

本插件**自身不进行语音合成**，它依赖一个后端的 **Genie TTS 服务**。您必须先拥有一个可访问的该服务，插件才能正常工作。

> **Genie TTS** 是一个强大的语音合成项目，您需要将其部署为一个Web服务。
> - **官方仓库**: [https://github.com/High-Logic/Genie](https://github.com/High-Logic/Genie)

## 方案一：使用 Hugging Face 一键部署

算力免费而且无需本地机器配置，但是合成速度比较慢。

1.  **复制我的 Space**:
    -   服务仓库: [https://huggingface.co/spaces/clown145/genie-tts-t/tree/main](https://huggingface.co/spaces/clown145/genie-tts-t/tree/main)
    -   点击页面右上角的 **"Duplicate this Space"** 即可一键复制，拥有一个完全属于您自己的、免费的TTS服务。

2.  **使用自定义模型**:
    -   默认服务会从示例模型仓库 [clown145/my-genie-tts-models](https://huggingface.co/clown145/my-genie-tts-models/tree/main) 下载模型。该模型仓库已包含多个预置角色，例如： `kisaki` (月社妃), `hiy` (和泉妃爱), `may` (椎名真由理), `aoi` (葵) 等，您可以直接使用。现在默认注册三个角色是kisaki,aoi,oka。您可以去app.py按照说明修改。
    -   若要使用您自己的模型，请将您训练和转换好的模型上传到您自己的 Hugging Face 模型仓库，然后在 Space 的 `app.py` 文件中修改 `REPO_ID` 和 `CHARACTERS` 字典。
    -   **【关键步骤】** 在您的空间中，**您必须创建一个名为 `reference_audio` 的文件夹**，并将所有用于注册情感的参考音频文件（如 `.wav`, `.ogg`）放入其中。
    -   **注意：** Genie 服务目前有加载3个模型的上限，请确保 `CHARACTERS` 字典中启用的角色不超过3个。
3.  **开启自动保活（可选）**：Hugging Face Space 超过 24 小时无人访问会休眠。插件提供了“自动保活空间”的开关，开启后会定时访问空间主页防止休眠。配置项：
    -   **启用**：在插件配置中打开“是否自动定期访问 Hugging Face Space 以防止休眠”。
    -   **保活地址**：默认使用 TTS 服务器列表的第一个地址，若您想单独设置，请填写“保活请求的目标地址”。
    -   **间隔**：可通过“两次保活之间的间隔分钟数”调整访问频率，建议 15-30 分钟。

## 方案二：本地或 Windows 部署

- 如果您想在本地运行，请参照 Genie 官方仓库的文档进行部署。
- 作者还提供了 **Windows 一键整合包**，极大简化了部署流程，详情请访问其 GitHub。

**部署完成后，请记下您的服务 URL (例如 `https://your-name-your-space.hf.space`)，后续配置插件时需要用到。**

---

[← 文档索引](README.md) · [项目主页](../README.md)
