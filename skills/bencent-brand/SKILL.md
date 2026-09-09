---
name: Bencent
description: 數位工作室，專注 AI 建站、WordPress + Greenshift 開發、自動化工作流。風格融合日式極簡與編輯精準感，以大地色調取代 AI 生成感的鮮豔配色。
colors:
  primary: "#2C2420"
  secondary: "#8B5E3C"
  tertiary: "#C4A882"
  neutral: "#F5F1EB"
  surface: "#EDE8E1"
  muted: "#9E9589"
typography:
  h1:
    fontFamily: "Noto Serif TC"
    fontSize: 3.5rem
    fontWeight: 700
    letterSpacing: "-0.02em"
    lineHeight: 1.15
  h2:
    fontFamily: "Noto Serif TC"
    fontSize: 2rem
    fontWeight: 600
    letterSpacing: "-0.01em"
  body-md:
    fontFamily: "Noto Sans TC"
    fontSize: 1rem
    fontWeight: 400
    lineHeight: 1.75
  label:
    fontFamily: "Noto Sans TC"
    fontSize: 0.75rem
    fontWeight: 500
    letterSpacing: "0.08em"
rounded:
  sm: 2px
  md: 4px
  lg: 8px
spacing:
  xs: 4px
  sm: 8px
  md: 16px
  lg: 32px
  xl: 64px
  2xl: 128px
components:
  button-primary:
    backgroundColor: "{colors.secondary}"
    textColor: "#FFFFFF"
    rounded: "{rounded.sm}"
    padding: "12px 24px"
  button-primary-hover:
    backgroundColor: "#6B4A2E"
    textColor: "#FFFFFF"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.secondary}"
    rounded: "{rounded.sm}"
    padding: "12px 24px"
  card:
    backgroundColor: "{colors.neutral}"
    rounded: "{rounded.md}"
    padding: "{spacing.lg}"
  card-border:
    backgroundColor: "{colors.neutral}"
    rounded: "{rounded.md}"
    padding: "{spacing.lg}"
  tag:
    backgroundColor: "transparent"
    textColor: "{colors.tertiary}"
    rounded: "{rounded.sm}"
    padding: "4px 10px"
  section-alt:
    backgroundColor: "{colors.surface}"
---

## Overview

Bencent 是一個數位工作室品牌，定位在「懂技術的設計師」與「懂設計的開發者」之間。視覺識別的核心目標是：**去除 AI 生成感，建立手感與信任感**。

風格參照：日本職人工作室、獨立出版物、Notion 文件美學、Linear 的精準感。

整體感受：煙燻紙張、印刷墨水、低調沉著、閱讀友善。

## Colors

大地色調為主軸，完全捨棄鮮豔橘紅與純白純黑。

- **Primary (#2C2420)** 墨炭：帶深棕底蘊的黑，用於所有正文與大標題。比純黑更溫潤，印刷感強。
- **Secondary (#8B5E3C)** 赭石：唯一的 CTA 色，用於主要按鈕、關鍵字 highlight、重要連結。內斂但對比夠高。
- **Tertiary (#C4A882)** 生土：點綴色，用於標籤、卡片邊框、icon、分隔線。不做文字色（對比不足）。
- **Neutral (#F5F1EB)** 煙燻白：全站畫布。帶微黃暖意，長時間閱讀不刺眼，有書卷氣。
- **Surface (#EDE8E1)** 淡燧石：次要背景，用於交錯 Section 的區塊分隔。比煙燻白略深，但保持輕盈。
- **Muted (#9E9589)** 燧石灰：輔助色，只用於細節（placeholder、disabled 狀態、hover 底色）。不做大面積背景。

## Typography

以中文優先為設計基礎，搭配簡潔無裝飾的英文字型。

- **標題**：Noto Serif TC，大字、緊字距，製造印刷版面感
- **內文**：Noto Sans TC，1.75 行高，閱讀友善
- **標籤/說明文字**：Noto Sans TC，0.75rem，大字距，製造精緻感

絕對不使用：圓體、可愛字型、過多字重混搭。

## Layout

日式編輯版面邏輯：大量留白，元素精簡，視覺動線清晰。

- 最大寬度：1200px
- 標準側邊留白：min(3vw, 20px)
- Section 垂直間距：64px（手機）/ 128px（桌機）
- 內容區塊：12 格網格，文字區塊最多佔 7 格

版面節奏：**一個 Section 只說一件事**。不要把三個訊息擠在同一個區塊。

## Elevation & Depth

無陰影系統。深度靠色塊交錯和留白製造，不靠 box-shadow。

唯一例外：卡片 hover 時可用極淡陰影 `0 2px 8px rgba(44,36,32,0.08)`。

## Shapes

極簡幾何，不使用裝飾性圖形。

- 圓角：最大 8px，按鈕用 2px（幾乎直角）
- 分隔線：1px solid #C4A882，不用 hr，改用 border-bottom
- 插圖方向：手繪線條感、黑白或低飽和、有微粒感（Grain），避免 3D AI 罐頭圖

## Components

按鈕必須直角感（rounded: 2px），赭石底白字為主要 CTA，次要按鈕用透明底赭石邊框。卡片無陰影，用 surface 色背景或細邊框區隔。標籤用生土色文字，透明底。

## Do's and Don'ts

**Do：**
- 大標題用 Noto Serif TC，製造版面重心
- 留白要多，寧可少元素
- 圖片選帶有質感、低飽和、職人風格的
- CTA 按鈕全站統一用赭石

**Don't：**
- 不用純黑 #000000 或純白 #FFFFFF
- 不用鮮豔橘紅、螢光色、漸層
- 不用 3D AI 插圖（幾何方塊、漸層球體）
- 不用圓角超過 8px 的按鈕
- 不在同一個 Section 放超過兩個 CTA
- 不用燧石灰做大面積背景
