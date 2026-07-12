import SwiftUI

struct LogsDrawer: View {
    @ObservedObject var model: AppModel

    var body: some View {
        GroupBox {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 2) {
                        if model.logs.isEmpty {
                            Text("Application activity and errors will appear here.")
                                .foregroundStyle(.secondary)
                        }
                        ForEach(model.logs) { entry in
                            Text(entry.line)
                                .font(.system(.caption, design: .monospaced))
                                .textSelection(.enabled)
                                .id(entry.id)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    .padding(6)
                }
                .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 5))
                .onChange(of: model.logs.count) { _, _ in
                    if let last = model.logs.last {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
            }
        } label: {
            HStack {
                Text("Application Logs")
                Spacer()
                Button("Clear") { model.clearLogs() }
                    .controlSize(.small)
            }
        }
    }
}
