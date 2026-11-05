import QtQuick
import QtQuick.Controls
import FluentUI
import QtQuick.Layouts
import "./ModelParse.js" as MP

Item {
    id: settingsRoot
    property string settingsData: ""
    property var settingsPayload: ({})
    property var renderedGroups: []
    property string uiLocale: typeof translator !== "undefined" ? translator.locale : Qt.locale().name
    onUiLocaleChanged: {
        if(Object.keys(settingsPayload).length) renderSettings()
    }
    property string argsData: ""
    property string valueData: ""
    // 存入 配置名和 任务名两个 上下文
    property string configName: ""
    property string taskName: ""
    signal updataData
    FluScrollablePage{
        id: contentScrollable
        width: 700
        height: parent.height
        spacing: 12
        anchors.horizontalCenter: parent.horizontalCenter
    }

    Component{
        id: group_item
        FluExpander{
            id: expander
            property string groupName: ""
            property string groupTitle: ""
            property var argumentValue: {""}
            property var fieldItems: []
            property var variants: []
            expand: true
            headerText: qsTr(groupTitle) +" "+ qsTr("Setting")
            width: contentScrollable.width
            contentHeight: group_column.height + 20
            Column{
                id: group_column
                spacing: 4
                clip: true
                anchors{
                    top: parent.top
                    topMargin: 12
                    left: parent.left
                    leftMargin: 15
                    right: parent.right
                    rightMargin: 15
                    bottomMargin: 12
                }
                FComboBox {
                    visible: expander.variants.length > 0
                    height: visible ? implicitHeight : 0
                    width: 300
                    model: expander.variants.map(function(v) { return settingsRoot.localized(v.label) })
                    onActivated: expander.argumentValue = settingsRoot.rowsForSources(expander.variants[currentIndex].groups)
                }
            }
            onArgumentValueChanged: {
                fieldItems.forEach(function(item) { item.destroy() })
                fieldItems = []
                for(let index in argumentValue){
                    const arg = argumentValue[index]
                    createItem(arg)
//                    expander.contentHeight = group_column.height
                }
            }
            /**
             * 创建某个具体的字段
             * @param {Object} arg 打包的变量大概长这样的：
                {
                    "name": "handle_error",
                    "title": "Handle Error",
                    "description": "none",
                    "default": false,
                    "type": "boolean",
                    "value": false
                }
             * @returns {Object}
             */
            function createItem(arg){
                if(arg === null){
                    console.error("it is emtry when create arg")
                    return
                }

                var object = null
                if(arg.ui_hidden) return
                if(arg.type === "string"){
                    object = string_item.createObject(group_column)
                    if(object === null){
                        console.error("can not create component string_item", arg.title)
                        return
                    }
                }
                else if(arg.type === "integer"){
                    object = int_item.createObject(group_column)
                    if(object === null){
                        console.error("can not create component integer_item", arg.title)
                        return
                    }
                }
                else if(arg.type === "number"){
                    object = float_item.createObject(group_column)
                    if(object === null){
                        console.error("can not create component float_item", arg.title)
                        return
                    }
                }
                else if(arg.type === "boolean"){
                    object = bool_item.createObject(group_column)
                    if(object === null){
                        console.error("can not create component boolean_item", arg.title)
                        return
                    }
                }
                else if(arg.type === "enum"){
                    object = enum_item.createObject(group_column)
                    if(object === null){
                        console.error("can not create component enum_item", arg.title)
                        return
                    }
                }
                else if(arg.type === "multi_enum"){
                    object = multi_enum_item.createObject(group_column)
                }
                else if(arg.type === "multi_line"){
                    object = multi_line_item.createObject(group_column)
                    if(object === null){
                        console.error("can not create component multi_line_item", arg.title)
                        return
                    }
                }


                if(object === null){
                    console.error("can not create component ", arg.title)
                    return
                }
                object.modelData = arg
                fieldItems.push(object)
            }
        }

    }

    Component {
        id: multi_enum_item
        Column {
            id: selection
            property var modelData: null
            width: parent.width
            spacing: 6
            FluText {
                text: selection.modelData ? selection.modelData.title : ""
                font: FluTextStyle.BodyStrong
            }
            Flow {
                width: parent.width
                spacing: 12
                Repeater {
                    model: selection.modelData ? selection.modelData.options : []
                    FluCheckBox {
                        text: selection.modelData.optionLabels[index]
                        selected: selection.modelData.value.indexOf(modelData) >= 0
                        clickFunc: function() {
                            var row = selection.modelData
                            var option = modelData
                            var values = settingsRoot.selectionValues(row, option, !selected)
                            if(set_task(row.sourceGroup, row.name, JSON.stringify(values))) {
                                var updated = Object.assign({}, row)
                                updated.value = values
                                selection.modelData = updated
                            }
                        }
                    }
                }
            }
        }
    }

    Component{
        id: string_item
        Item{
            id: stringItem
            property var modelData: null
            property bool isDateTime: false
            width: parent.width
            implicitHeight: string_title.implicitHeight + string_description.implicitHeight
            Layout.fillHeight: true
            FluTextBox{
                id: string_text
                anchors{
                    top: parent.top
                    topMargin: 2
                    right: parent.right
                }
                placeholderText: ""
                onEditingFinished: {
                    if(stringItem.isDateTime){
                        if(string_text.text === ""){
                            string_text.text = utils.current_datetime()
                        }
                    }

                    if(set_task(modelData.sourceGroup || stringItem.parent.parent.parent.groupName, modelData.name, text)){
                        showSuccess( qsTr("Successful setting"))
                    }
                }
            }

            FluText{
                id: string_title
                anchors{
                    top: parent.top
                    left: parent.left
                    right: string_text.left
                }
                text: ""
                font: FluTextStyle.BodyStrong
                wrapMode: Text.WrapAnywhere
                topPadding: 6
            }
            FluText{
                id: string_description
                anchors{
                    top: string_title.bottom
                    left: parent.left
                }
                width: string_title.width
                text: ""
                font: FluTextStyle.Caption
                wrapMode: Text.WordWrap
//                textFormat: Text.PlainText
                topPadding: 6
                rightPadding: 6
            }
            onModelDataChanged: {
                if(modelData === null){
                    console.error("string model is null")
                    return
                }
                string_text.text = modelData.value
                string_text.placeholderText = modelData["default"]
                if(string_text.placeholderText.startsWith("2023-01-01")){
                    //判断是不是时间类型的变量2023-01-01
                    stringItem.isDateTime = true
                }
                string_title.text = qsTr(modelData.title)
                if( typeof modelData.description === 'undefined'){
                    return
                }
                string_description.text = qsTr(modelData.description.replace("\\n","\n"))
            }
        }
    }
    Component{
        id: int_item
        Item{
            id: itemArg
            property var modelData: null
            width: parent.width
            implicitHeight: itemArg_title.implicitHeight + itemArg_description.implicitHeight
            Layout.fillHeight: true
            FluTextBox{
                id: itemArg_text
                anchors{
                    top: parent.top
                    topMargin: 2
                    right: parent.right
                }
                validator: IntValidator {}
                placeholderText: ""
                onEditingFinished: {
                    if(set_task(modelData.sourceGroup || itemArg.parent.parent.parent.groupName, modelData.name, parseInt(text))){
                        showSuccess( qsTr("Successful setting"))
                    }
                }
            }

            FluText{
                id: itemArg_title
                anchors{
                    top: parent.top
                    left: parent.left
                    right: itemArg_text.left
                }
                text: ""
                font: FluTextStyle.BodyStrong
                wrapMode: Text.WrapAnywhere
                topPadding: 6
            }
            FluText{
                id: itemArg_description
                anchors{
                    top: itemArg_title.bottom
                    left: parent.left
                }
                width: itemArg_title.width
                text: ""
                font: FluTextStyle.Caption
                wrapMode: Text.WrapAnywhere
//                textFormat: Text.PlainText
                topPadding: 6
                rightPadding: 6
            }
            onModelDataChanged: {
                if(modelData === null){
                    console.error("int model is null")
                    return
                }
                itemArg_text.text = modelData.value
                itemArg_text.placeholderText = modelData["default"]
                itemArg_title.text = qsTr(modelData.title)
                if( typeof modelData.description === 'undefined'){
                    return
                }
                itemArg_description.text = qsTr(modelData.description)
            }
        }
    }
    Component{
        id: float_item
        Item{
            id: itemArg
            property var modelData: null
            width: parent.width
            implicitHeight: itemArg_title.implicitHeight + itemArg_description.implicitHeight
            Layout.fillHeight: true
            FluTextBox{
                id: itemArg_text
                anchors{
                    top: parent.top
                    topMargin: 2
                    right: parent.right
                }
                validator: DoubleValidator {}
                placeholderText: ""
                onEditingFinished: {
                    if(set_task(modelData.sourceGroup || itemArg.parent.parent.parent.groupName, modelData.name, parseFloat(text))){
                        showSuccess( qsTr("Successful setting"))
                    }
                }
            }

            FluText{
                id: itemArg_title
                anchors{
                    top: parent.top
                    left: parent.left
                    right: itemArg_text.left
                }
                text: ""
                font: FluTextStyle.BodyStrong
                wrapMode: Text.WrapAnywhere
                topPadding: 6
            }
            FluText{
                id: itemArg_description
                anchors{
                    top: itemArg_title.bottom
                    left: parent.left
                }
                width: itemArg_title.width
                text: ""
                font: FluTextStyle.Caption
                wrapMode: Text.WrapAnywhere
//                textFormat: Text.PlainText
                topPadding: 6
                rightPadding: 6
            }
            onModelDataChanged: {
                if(modelData === null){
                    console.error("float model is null")
                    return
                }
                itemArg_text.text = modelData.value
                itemArg_text.placeholderText = modelData["default"]
                itemArg_title.text = qsTr(modelData.title)
                if( typeof modelData.description === 'undefined'){
                    return
                }
                itemArg_description.text = qsTr(modelData.description)
            }
        }
    }
    Component{
        id: bool_item
        Item{
            id: itemArg
            property var modelData: null
            width: parent.width
            implicitHeight: itemArg_title.implicitHeight + itemArg_description.implicitHeight
            Layout.fillHeight: true
            FluCheckBox{
                id: itemArg_text
                anchors{
                    top: parent.top
                    topMargin: 2
                    right: parent.right
                    rightMargin: 300 - 24
                }
                selected: true
                clickFunc: function click_func(){
                    selected = !selected
                    if(set_task(modelData.sourceGroup || itemArg.parent.parent.parent.groupName, modelData.name, selected)){
                        showSuccess( qsTr("Successful setting"))
                    }
                }
            }

            FluText{
                id: itemArg_title
                anchors{
                    top: parent.top
                    left: parent.left
                    right: itemArg_text.left
                }
                text: ""
                font: FluTextStyle.BodyStrong
                wrapMode: Text.WrapAnywhere
                topPadding: 6
            }
            FluText{
                id: itemArg_description
                anchors{
                    top: itemArg_title.bottom
                    left: parent.left
                }
                width: itemArg_title.width
                text: ""
                font: FluTextStyle.Caption
                wrapMode: Text.WrapAnywhere
                topPadding: 6
                rightPadding: 6
            }
            onModelDataChanged: {
                if(modelData === null){
                    console.error("bool model is null")
                    return
                }
                itemArg_text.selected = modelData.value
                itemArg_title.text = qsTr(modelData.title)
                if( typeof modelData.description === 'undefined'){
                    return
                }
                itemArg_description.text = qsTr(modelData.description)
            }
        }
    }
    Component{
        id: enum_item
        Item{
            id: itemArg
            property var modelData: null
            width: parent.width
            implicitHeight: itemArg_title.implicitHeight + itemArg_description.implicitHeight
            Layout.fillHeight: true
            FComboBox{
                id: itemArg_text
                width: 300
                anchors{
                    top: parent.top
                    topMargin: 2
                    right: parent.right
                }
                model: ["0", "1", "2"]
                onActivated: {
                    if(set_task(modelData.sourceGroup || itemArg.parent.parent.parent.groupName, modelData.name, modelData.options[currentIndex])){
                        showSuccess( qsTr("Successful setting"))
                    }
                }
            }

            FluText{
                id: itemArg_title
                anchors{
                    top: parent.top
                    left: parent.left
                    right: itemArg_text.left
                }
                text: ""
                font: FluTextStyle.BodyStrong
                wrapMode: Text.WrapAnywhere
                topPadding: 6
            }
            FluText{
                id: itemArg_description
                anchors{
                    top: itemArg_title.bottom
                    left: parent.left
                }
                width: itemArg_title.width
                text: ""
                font: FluTextStyle.Caption
                wrapMode: Text.WrapAnywhere
//                textFormat: Text.PlainText
                topPadding: 6
                rightPadding: 6
            }
            onModelDataChanged: {
                if(modelData === null){
                    console.error("enum model is null")
                    return
                }
                itemArg_text.model = modelData.optionLabels || modelData.options
                itemArg_text.currentIndex = modelData.options.indexOf(modelData.value)
                itemArg_title.text = qsTr(modelData.title)
                if( typeof modelData.description === 'undefined'){
                    return
                }
                itemArg_description.text = qsTr(modelData.description)
            }
        }
    }
    Component{
        id: multi_line_item
        Item{
            id: multiLineItem
            property var modelData: null
            width: parent.width
            implicitHeight: string_title.implicitHeight + string_description.implicitHeight
            height: if(string_text.height > (string_title.implicitHeight + string_description.implicitHeight)){
                        return string_text.height
                    }else{
                        return string_title.implicitHeight + string_description.implicitHeight
                    }

            Layout.fillHeight: true
            FluMultilineTextBox{
                id: string_text
                anchors{
                    top: parent.top
                    topMargin: 2
                    right: parent.right
                }
                placeholderText: ""
                onEditingFinished: {
                    if(set_task(modelData.sourceGroup || multiLineItem.parent.parent.parent.groupName, modelData.name, text)){
                        showSuccess( qsTr("Successful setting"))
                    }
                }
//                onTextChanged: {
//                    if(set_task(modelData.sourceGroup || multiLineItem.parent.parent.parent.groupName, modelData.name, text)){
//                        showSuccess( qsTr("Successful setting"))
//                    }
//                }
            }

            FluText{
                // 设置的名称
                id: string_title
                anchors{
                    top: parent.top
                    left: parent.left
                    right: string_text.left
                }
                text: ""
                font: FluTextStyle.BodyStrong
                wrapMode: Text.WrapAnywhere
                topPadding: 6
            }
            FluText{
                //描述
                id: string_description
                anchors{
                    top: string_title.bottom
                    left: parent.left
                }
                width: string_title.width
                text: ""
                font: FluTextStyle.Caption
                wrapMode: Text.WordWrap
                topPadding: 6
                rightPadding: 6
            }
            onModelDataChanged: {
                if(modelData === null){
                    console.error("string model is null")
                    return
                }
                string_text.text = modelData.value
                string_text.placeholderText = modelData["default"]

                string_title.text = qsTr(modelData.title)
                if( typeof modelData.description === 'undefined'){
                    return
                }
                string_description.text = qsTr(modelData.description.replace("\\n","\n"))
            }
        }
    }
    onArgsDataChanged: {

    }
    onValueDataChanged: {

    }
    onUpdataData: {
        if(settingsData !== "") {
            settingsPayload = JSON.parse(settingsData)
            renderSettings()
            return
        }
        if(argsData === ""){
            console.error("解析 顺序出问题了")
            return
        }
        if(valueData === ""){
            console.debug("没有数据")
        }

        const args = JSON.parse(argsData)
        const values = JSON.parse(valueData)
        const group = MP.parseGroup(args["properties"])
        const groups = MP.parseGroups(args["properties"])

        for(let task in group){
            const argument = MP.parseArgument(args.definitions, group[task])
            const groupLetter = groups[group[task]]
            const argumentVuale = MP.mergeArgument(argument, values[groupLetter])
//            console.debug(JSON.stringify( argumentVuale ))
//            console.debug("---------------------------------------------------------------")
            createGroup(groupLetter, group[task], argumentVuale)
        }
    }

    /**
     * 创建某个组 的显示组件并显示
     * @param {Object} groupName 变量的名字 是小写的
     * @param {Object} groupTitle 用于向用户显示的名字 大写的
     * @param {Object} argumentVaule 总的显示的参数
     * @returns {Object}
     */
    function createGroup(groupName, groupTitle, argumentValue, variants){
        if(!groupName){
            console.error("no group name")
            return
        }
        if(!groupTitle){
            console.error("no group title")
            return
        }

        const object = group_item.createObject(contentScrollable)
        if(object !== null){
            object.groupName = groupName
            object.groupTitle = qsTr(groupTitle)
            object.variants = variants || []
            object.argumentValue = argumentValue

            contentScrollable.content.push(object)
            renderedGroups.push(object)
        }
        else{
            console.error('create group item failed!')
        }
    }

    function localized(value) {
        if(typeof value === "string") return value
        if(!value) return ""
        var locale = uiLocale.replace(/-/g, "_")
        if(value[locale] !== undefined) return value[locale]
        var language = locale.split("_")[0].toLowerCase()
        var keys = Object.keys(value)
        var matching = keys.filter(function(key) { return key.split("_")[0].toLowerCase() === language })
        return value[matching[0] || keys[0]] || ""
    }

    function selectionValues(row, option, enabled) {
        var values = row.value.filter(function(item) { return item !== option })
        if(enabled) {
            ;(row.ui_exclusive_groups || []).forEach(function(group) {
                if(group.indexOf(option) >= 0)
                    values = values.filter(function(item) { return group.indexOf(item) < 0 })
            })
            values.push(option)
        }
        return values
    }

    function rowsForSources(sources) {
        var rows = []
        ;(sources || []).forEach(function(source) {
            ;(settingsPayload[source.name] || []).forEach(function(original) {
                if(original.ui_hidden || (source.fields && source.fields.indexOf(original.name) < 0)) return
                var row = Object.assign({}, original)
                row.sourceGroup = source.name
                var translated = row.ui_text ? localized(row.ui_text) : null
                if(translated) {
                    row.title = translated.title
                    row.description = translated.description
                }
                if(source.labels && source.labels[row.name]) row.title = localized(source.labels[row.name])
                row.options = row.enumEnum || row.options || []
                row.optionLabels = row.options.map(function(option) {
                    return translated && translated.options ? (translated.options[option] || option) : option
                })
                if(["time", "time_delta", "date_time"].indexOf(row.type) >= 0) row.type = "string"
                rows.push(row)
            })
        })
        return rows
    }

    function renderSettings() {
        renderedGroups.forEach(function(group) { group.destroy() })
        renderedGroups = []
        var controls = settingsPayload.settings_layout_config || []
        var layout = controls.filter(function(row) { return row.name === "merged_settings_enable" })[0]
        var used = {}
        if(layout && layout.value && layout.ui_layout) {
            layout.ui_layout.sections.forEach(function(section) {
                var variants = section.variants || []
                var sources = variants.length ? variants[0].groups : section.groups
                createGroup(section.id, localized(section.title), rowsForSources(sources), variants)
                ;(section.groups || []).forEach(function(source) { used[source.name] = true })
                variants.forEach(function(variant) {
                    variant.groups.forEach(function(source) { used[source.name] = true })
                })
            })
        }
        Object.keys(settingsPayload).forEach(function(name) {
            if(used[name]) return
            var rows = rowsForSources([{name: name}])
            if(!rows.length) return
            var first = settingsPayload[name][0]
            createGroup(name, localized(first.ui_group_title) || name, rows)
        })
    }

    function set_task(group, arg, value){
        //如果是字符串
        if( typeof value == "string" ){
        if(process_manager.gui_set_task(configName, taskName, group, arg, value)){
            return true
        }
        else{
            return false
        }}

        //如果是整形的
        else if(typeof value == "number"){
        if(process_manager.gui_set_task_number(configName, taskName, group, arg, value)){
            return true
        }
        else{
            return false
        }}


        //如果是bool的
        else if(typeof value == "boolean"){
        if(process_manager.gui_set_task_bool(configName, taskName, group, arg, value)){
            return true
        }
        else{
            return false
        }}


    }
}
