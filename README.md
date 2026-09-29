# 奥维 18 级影像自动导出

在已登录的 Windows 桌面打开奥维后，运行：

```powershell
& 'D:\App\python_env\gis312\python.exe' .\index.py
```

本机的 `python` 命令没有配置到 PATH，所以直接使用 `config.json` 指定的解释器，无需 QGIS。程序最大化奥维，按目标图层名称寻找属性窗口，逐块导出 18 级 TIF，处理“已有地图缓存”和“先下载地图”两种分支。全部 TIF 校验通过后，按原始要素拼接并裁切。导出和下载可能需要较长时间，保持 Windows 桌面会话登录且不操作奥维。

## 配置

编辑 [`config.json`](config.json)：

| 字段 | 用途 |
| --- | --- |
| `target` | 下载目标。支持 `.geojson`、`.shp`、`.gpkg`。SHP/GeoJSON 处理所有面；GeoPackage 取第一个包含面的图层中的第一个面。 |
| `omapexepath` | 奥维主程序路径；若未发现已运行的 VIP 主窗口，入口自动启动并等待。 |
| `source_features` | 最终拼接裁切的原始面文件；当前指向 7 个原始图斑。 |
| `export_dir` | 每块 TIF 与 `ovi_export_ledger.json` 的目录。 |
| `mosaic_dir` | 最终各原始图斑 TIF 与 `mosaic_ledger.json` 的目录。 |
| `max_attempts` | 每块 UI 导出失败后的最大尝试次数。 |
| `mosaic` | 是否在全部导出并校验后拼接。自定义目标且不需要按原始图斑拼接时设为 `false`。 |

默认 `target` 是 `out/grid_3km_z18_buffer20.geojson`：在原来 40 块网格各自完成分配后，沿边界向外扩 20 米，让相邻导出块有重叠；最终 TIF 仍按原始 7 个图斑裁切。新块名称带 `_buf20p0m`，可以与已经导入奥维的旧网格并存。新导出写入 `out/vip_parts_buffer20`，新拼接结果写入 `out/features_z18_buffer20`；旧的 40 块 TIF 和 7 个成果保留。修改 `plan_buffer_meters` 或原始图斑后，须重新生成目标并使用新的 `export_dir`，旧目标的断点不能直接沿用。SHP 和 GeoPackage 会先转为 `prepared_target` 指定的 WGS84 GeoJSON，再导入奥维。SHP 需保留同名的 `.shx`、`.dbf`、`.prj` 文件；无坐标系的目标会报错。

检查配置和现有进度，不操作奥维：

```powershell
& 'D:\App\python_env\gis312\python.exe' .\index.py --check
```

只准备导入目标，不操作奥维：

```powershell
& 'D:\App\python_env\gis312\python.exe' .\index.py --prepare
```

`out/ovi_pipeline_buffer20_state.json` 记录当前阶段和最近错误；导出与拼接分别在各自目录写入断点台账。再次运行入口命令会验证已完成的 TIF，继续未完成的块。每块失败最多重试配置次数；仍失败时停止并保留状态，避免无止境循环。

若 40 个分块已经导出，只重新生成最终 7 幅成品，无需奥维或重新下载：

```powershell
& 'D:\App\python_env\gis312\python.exe' .\index.py --mosaic-only
```

奥维 TIF 的无数据白底主要编码为 RGB=(254,254,254)、第四波段 alpha=1。拼接时只有 alpha≥128 的像素可以覆盖邻块，并写为不透明；最终输出在原始要素边界之外透明。输出使用奥维默认坐标系，校验为 EPSG:3857。原始目标网格由 `src/export_plan.py` 生成，3 km 网格中小于 0.7 km² 的边缘碎片会并入相邻小块。

oamp版本，10.7.2
