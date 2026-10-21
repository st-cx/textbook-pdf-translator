// Textbook compositor. No font shrinking or formula fitting is permitted.
// All strings in the generated source are escaped Python JSON strings.

#let trace(kind, id, phase, width, source-page: 0, source-label: "", element-id: "", height: 0pt, column-count: 1, render-format: "", flow-id: "") = context {
  let p = here().position()
  [#metadata((
    type: kind, id: id, phase: phase, page: p.page,
    x: p.x / 1pt, y: p.y / 1pt, width: width / 1pt,
    height: height / 1pt, source_page: source-page,
    source_label: source-label, element_id: element-id,
    columns: column-count,
    render_format: render-format, flow: flow-id,
  )) <tb-trace>]
}

#let page-sources() = context {
  let page = here().position().page
  let records = query(<tb-trace>).map(it => it.value)
  let starts = records.filter(it => it.type == "element" and it.phase == "start")
  let ends = (:)
  for item in records.filter(it => it.type == "element" and it.phase == "end") {
    ends.insert(item.id, item.page)
  }
  let matching = starts.filter(it => {
    let endpoint = ends.at(it.id, default: page)
    it.page <= page and endpoint >= page
  })
  let labels = matching.map(it => it.source_label).dedup()
  if labels.len() > 0 {
    let label = labels.join(" · ")
    if labels.len() > 1 and labels.all(it => it.match(regex("^原书 p\\.\\d+$")) != none) {
      let numbers = labels.map(it => int(it.replace("原书 p.", ""))).sorted().dedup()
      if numbers.last() - numbers.first() == numbers.len() - 1 {
        label = "原书 pp." + str(numbers.first()) + "–" + str(numbers.last())
      }
    }
    text(size: 7.5pt, fill: rgb("647481"), label)
  }
}

#let book-style(settings, body) = {
  set page(
    width: settings.width * 1pt, height: settings.height * 1pt,
    margin: (left: settings.left * 1pt, right: settings.right * 1pt,
             top: settings.top * 1pt, bottom: settings.bottom * 1pt),
    footer: align(right)[#page-sources()], footer-descent: 16pt,
    header: if settings.preview {
      align(right)[
        #text(size: 8pt, fill: rgb("b83a24"), "未完成翻译或解析预览")
        #if settings.source_error_summary != "" {
          linebreak()
          text(size: 7.5pt, fill: rgb("b83a24"), settings.source_error_summary)
        }
      ]
    } else { none },
  )
  set text(font: (settings.body_font, "Microsoft YaHei", "Libertinus Serif"),
           size: settings.body_size * 1pt, lang: "zh", region: "CN", fill: rgb("202730"),
           top-edge: "ascender", bottom-edge: "descender")
  set par(justify: true, leading: .55em, spacing: .65em, linebreaks: "optimized")
  show raw: set text(font: "Consolas", size: settings.body_size * 1pt)
  set block(spacing: 0pt)
  set heading(numbering: none)
  body
}

#let protected(id, element-id, path, width, height, descender, pdf-page: 1) = box(
  width: width, height: height, baseline: descender, clip: true,
)[#trace("asset", id, "object", width, height: height, element-id: element-id, render-format: path.split(".").last())#if path.ends-with(".pdf") {
  image(path, width: width, height: height, page: pdf-page)
} else { image(path, width: width, height: height) }]

// Decisions are measured once by Python, then frozen for the final compile.
// Never condition a break on its own live balanced-column position.
#let baseline-probe(kind, id) = box(width: 0pt, height: 0pt, baseline: 0pt)[
  #trace(kind, id, "baseline", 0pt)
]

#let source-column-boundary(id, break-before: false) = {
  trace("column-boundary", id, "before", 0pt)
  if break-before { colbreak(weak: true) }
}

#let exercise-column-candidate(id, flow-id, break-before: false) = {
  trace("exercise-boundary", id, "before", 0pt, flow-id: flow-id)
  if break-before { colbreak(weak: true) }
}

#let source-segment-start(label) = block(above: 0pt, below: 8pt, sticky: true)[
  #text(size: 8.5pt, fill: rgb("647481"), label)
]

#let contents-row(page, body) = block(breakable: false)[
  #set par(justify: false)
  #grid(columns: (auto, 1fr, auto), column-gutter: 6pt,
    [#body], [#if page != "" { repeat[.] }], [#page])
]

#let inline-source-group(identities, width, heading: false, contents: false, column-count: 1, body) = {
  block(width: width, breakable: true, sticky: heading, above: if heading { 10pt } else { 0pt }, below: if contents { 2pt } else { 5pt })[
    #for item in identities {
      trace("element", item.id, "start", width, source-page: item.source-page,
            source-label: item.source-label, column-count: column-count)
    }
    #body
    #for item in identities {
      trace("element", item.id, "end", width, source-page: item.source-page,
            source-label: item.source-label, column-count: column-count)
    }
  ]
}

#let margin-source(identities, width, size, body) = block(width: width, breakable: false)[
  #for item in identities {
    trace("element", item.id, "start", width, source-page: item.source-page, source-label: item.source-label)
  }
  #set text(size: size, fill: rgb("009ac6"))
  #set par(justify: false, leading: .35em)
  #align(right)[#body]
  #for item in identities {
    trace("element", item.id, "end", width, source-page: item.source-page, source-label: item.source-label)
  }
]

#let banner-source(id, source-page, source-label, width, height, body) = block(width: width, height: height)[
  #trace("element", id, "start", width, source-page: source-page, source-label: source-label)
  #place(top + left)[#body]
  #place(top + left, dy: height)[#trace("element", id, "end", width, source-page: source-page, source-label: source-label)]
]

#let tracked(id, kind, source-page, source-label, width, size,
             heading: false, heading-font: "SimHei", heading-color: "009ac6", preview: false, exercise: false,
             keep: false, caption-center: true, column-count: 1, rule-below: false,
             subpart: false, sticky-next: false, banner: false, body) = {
  let gap = if banner { 8pt } else if heading { 8pt } else if kind == "caption" { 4pt } else if kind == "table_cell" { 0pt } else if subpart { 1pt } else { 5pt }
  block(width: width, breakable: not keep, sticky: heading or kind == "caption" or sticky-next, above: if heading and not banner { 10pt } else { 0pt }, below: gap)[
    #trace("element", id, "start", width, source-page: source-page, source-label: source-label, column-count: column-count)
    #set text(size: size)
    #set par(first-line-indent: 0pt, hanging-indent: if exercise { 1.6em } else { 0pt })
    #if preview { text(size: 8pt, fill: rgb("b83a24"), weight: "bold", "【未翻译原文】 ") }
    #if heading {
      text(font: (heading-font, "Microsoft YaHei"), weight: "bold", fill: rgb(heading-color), body)
    } else if kind == "caption" and caption-center { align(center, body) } else { body }
    #if rule-below { line(length: 100%, stroke: .5pt + rgb(heading-color)) }
    #trace("element", id, "end", width, source-page: source-page, source-label: source-label, column-count: column-count)
  ]
}
