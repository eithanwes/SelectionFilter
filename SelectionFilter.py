"""
/***************************************************************************
 SelectionFilter
                                 A QGIS plugin
 This plugin show selected features only.

 Base was written manually
                              -------------------
        begin                : 2023-08-24
        copyright            : (C) 2023 Kyaw Naing Win
        email                : kyawnaingwinknw@gmail.com
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
 """

import os

from qgis.core import (
    Qgis,
    QgsFeatureRequest,
    QgsFieldConstraints,
    QgsMapLayerType,
    QgsProject,
)
from qgis.PyQt.QtCore import QCoreApplication

#from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QAction, QIcon
from qgis.utils import iface

from .setUniqueField import setUniqueFieldPopup

# QGIS 4 requires the scoped enum; QGIS 3 exposes it unscoped.
NO_GEOMETRY = getattr(QgsFeatureRequest, "Flag", QgsFeatureRequest).NoGeometry
CONSTRAINT_UNIQUE = getattr(
    QgsFieldConstraints, "Constraint", QgsFieldConstraints).ConstraintUnique

# Counting distinct values is a full table scan, so only pay for it on layers
# small enough that the user will not notice the dialog stalling.
UNIQUE_CHECK_LIMIT = 100000

ICONS_DIR = os.path.join(os.path.dirname(__file__), "icons")

# The plugin's own icon; also declared as `icon=` in metadata.txt.
PLUGIN_ICON = "sf_plugin_icon.svg"


def plugin_icon(name: str = PLUGIN_ICON) -> QIcon:
    """Load one of the bundled icons from the plugin's icons folder."""
    return QIcon(os.path.join(ICONS_DIR, name))


class SelectionFilter:
    def __init__(self, iface):
        super().__init__()
        self.iface = iface

        self.create_filter_action = QAction(
            plugin_icon(), QCoreApplication.translate("filterSelected", "&Show selected feature(s) only"))
        self.create_hide_filter_action = QAction(
            plugin_icon("sf_hide_icon.svg"), QCoreApplication.translate("hideSelected", "&Hide selected feature(s)"))
        self.create_clear_filter_action = QAction(
            plugin_icon("sf_clear_icon.svg"), QCoreApplication.translate("clearFilterSelected", "&Clear filter"))
        self.set_unique_field_action = QAction(
            plugin_icon("sf_field_icon.svg"), QCoreApplication.translate("showSetUniqueFieldPopup", "Se&t field"))

    def initGui(self):    
        QgsProject.instance().layerWasAdded.connect(self.updateFilterActions)

        for layer in list(QgsProject.instance().mapLayers().values()):
            self.updateFilterActions(layer)
  
        self.create_filter_action.triggered.connect(self.filterSelected)
        self.create_hide_filter_action.triggered.connect(self.hideSelected)
        self.create_clear_filter_action.triggered.connect(self.clearFilterSelected)
        self.set_unique_field_action.triggered.connect(self.showSetUniqueFieldPopup)

        
    def unload(self):
        # 1) Disconnect project-level signals that reference our methods
        try:
            QgsProject.instance().layerWasAdded.disconnect(self.updateFilterActions)
        except Exception:
            pass

        self.iface.removeCustomActionForLayerType(self.create_filter_action)
        self.iface.removeCustomActionForLayerType(self.create_hide_filter_action)
        self.iface.removeCustomActionForLayerType(self.create_clear_filter_action)

        # 2) Disconnect QAction signals so slots won't be called after plugin unload
        try:
            self.create_filter_action.triggered.disconnect(self.filterSelected)
        except Exception:
            pass
        try:
            self.create_hide_filter_action.triggered.disconnect(self.hideSelected)
        except Exception:
            pass
        try:
            self.create_clear_filter_action.triggered.disconnect(self.clearFilterSelected)
        except Exception:
            pass
        try:
            self.set_unique_field_action.triggered.disconnect(self.showSetUniqueFieldPopup)
        except Exception:
            pass

        # 3) Remove per-layer custom actions that were added for each layer
        for layer in QgsProject.instance().mapLayers().values():
            try:
                self.iface.removeCustomActionForLayer(self.create_filter_action, layer)
            except Exception:
                pass
            try:
                self.iface.removeCustomActionForLayer(self.create_hide_filter_action, layer)
            except Exception:
                pass
            try:
                self.iface.removeCustomActionForLayer(self.create_clear_filter_action, layer)
            except Exception:
                pass
            try:
                self.iface.removeCustomActionForLayer(self.set_unique_field_action, layer)
            except Exception:
                pass

        # 4) Remove the actions registered for layer types (global registration)
        try:
            self.iface.removeCustomActionForLayerType(self.create_filter_action)
        except Exception:
            pass
        try:
            self.iface.removeCustomActionForLayerType(self.create_hide_filter_action)
        except Exception:
            pass
        try:
            self.iface.removeCustomActionForLayerType(self.create_clear_filter_action)
        except Exception:
            pass
        try:
            self.iface.removeCustomActionForLayerType(self.set_unique_field_action)
        except Exception:
            pass

        # 5) Close / delete any plugin dialogs still open to avoid dangling widgets
        try:
            if hasattr(self, 'popup') and self.popup:
                # close and schedule for deletion
                self.popup.reject()
                self.popup.deleteLater()
        except Exception:
            pass

      
    def _is_null(self, value) -> bool:
        """True for a NULL attribute on both QGIS 3 (QVariant) and QGIS 4 (None)."""
        if value is None:
            return True
        is_null = getattr(value, "isNull", None)
        return bool(is_null()) if callable(is_null) else False

    def _quote_value(self, value, field_type) -> str:
        """Render one attribute value as a provider-safe SQL literal."""
        if self._is_null(value):
            return "NULL"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return str(value)
        for to_python in ("toPyDateTime", "toPyDate", "toPyTime"):
            converter = getattr(value, to_python, None)
            if callable(converter):
                value = converter()
                break
        return "'" + str(value).replace("'", "''") + "'"

    def _literals(self, values, field_type) -> list:
        """Render values as SQL literals; NULL stays bare so clauses can spot it."""
        return [self._quote_value(value, field_type) for value in values]

    def _format_literals(self, field, literals, operator) -> str:
        """Build an IN / NOT IN clause from already-quoted literals."""
        ident = f'"{field}"'
        has_null = "NULL" in literals
        literals = [literal for literal in literals if literal != "NULL"]

        if operator == "NOT IN":
            if not literals:
                return f"{ident} IS NOT NULL" if has_null else "1 = 1"
            clause = f'{ident} NOT IN ({", ".join(literals)})'
            # `NULL NOT IN (...)` is NULL, never true, so NULL-keyed rows need an
            # explicit rule or they silently vanish from the layer.
            if has_null:
                return f"({clause} AND {ident} IS NOT NULL)"
            return f"({clause} OR {ident} IS NULL)"

        if not literals:
            return f"{ident} IS NULL" if has_null else "1 = 0"
        clause = f'{ident} IN ({", ".join(literals)})'
        return f"({clause} OR {ident} IS NULL)" if has_null else clause

    def _format_query(self, field, field_type, values, operator) -> str:
        return self._format_literals(field, self._literals(values, field_type), operator)

    def _unique_values(self, layer, field, fids=None, exclude=None, cap=None):
        """Distinct values of `field`, read without geometry or other attributes.

        `exclude` drops feature ids from the result, which replaces inverting the
        selection. `cap` aborts once more than `cap` distinct values are found and
        returns None, meaning the other side of the comparison is the shorter one.
        """
        idx = layer.fields().indexOf(field)
        request = QgsFeatureRequest()
        request.setFlags(NO_GEOMETRY)
        request.setSubsetOfAttributes([idx])
        if fids is not None:
            request.setFilterFids(list(fids))

        values = set()
        for feature in layer.getFeatures(request):
            if exclude and feature.id() in exclude:
                continue
            value = feature[idx]
            values.add(None if self._is_null(value) else value)
            if cap is not None and len(values) > cap:
                return None
        return values

    def _verify_unique(self, layer, field) -> bool:
        """Check -- cheaply where possible -- that `field` identifies one feature."""
        try:
            idx = layer.fields().indexOf(field)
            if idx < 0:
                return False
            if idx in layer.primaryKeyAttributes():
                return True
            if layer.fields().at(idx).constraints().constraints() & CONSTRAINT_UNIQUE:
                return True
            if layer.featureCount() > UNIQUE_CHECK_LIMIT:
                return False
            return len(layer.uniqueValues(idx)) == layer.featureCount()
        except Exception:
            # Unverified only costs the IN/NOT IN swap, so fail closed and quietly.
            return False

    def _swap_allowed(self, layer) -> bool:
        """Swapping IN for NOT IN is only equivalent if the field is truly unique."""
        return bool(layer.customProperty("unique_field_verified", False))

    def _state_properties(self, layer):
        """Read back the filter this plugin last wrote.

        Returns (base, include_clause, hidden_literals). If the subset string no
        longer matches what the plugin wrote, someone edited the filter by hand,
        so the current string becomes the new base and the accumulators reset.
        """
        current = layer.subsetString()
        if current != layer.customProperty("sf_last_subset", None):
            return current, "", []
        hidden = layer.customProperty("sf_hidden", []) or []
        if isinstance(hidden, str):  # Qt collapses a one-item QStringList
            hidden = [hidden]
        return (layer.customProperty("sf_base", ""),
                layer.customProperty("sf_include", ""),
                list(hidden))

    def _compose(self, base, include, hidden_clause) -> str:
        """Join the at most three clauses the plugin ever emits."""
        parts = []
        if base:
            parts.append(f"({base})")
        if include:
            parts.append(include)
        if hidden_clause:
            parts.append(hidden_clause)
        return "\nAND ".join(parts) if parts else "1 = 1"

    def _build_query(self, layer, field, field_type, show_selected) -> str:
        base, include, hidden = self._state_properties(layer)
        selected_ids = set(layer.selectedFeatureIds())
        selected = self._unique_values(layer, field, fids=selected_ids)

        # The complement costs a full pass, so only pay for it when the swap is
        # valid, and abort the pass as soon as it is provably the longer side.
        rest = None
        if self._swap_allowed(layer):
            rest = self._unique_values(layer, field, exclude=selected_ids,
                                       cap=len(selected))
        rest_is_shorter = rest is not None and len(rest) < len(selected)
        # `rest` is the complement *within the current view*, so a whole-layer
        # NOT IN is equivalent only while nothing constrains the layer yet --
        # neither the user's own filter nor one this plugin already applied.
        # Narrowing an existing filter otherwise widens it back to the layer.
        unconstrained = not layer.subsetString()

        if show_selected:
            # A show replaces everything the plugin had accumulated.
            if rest_is_shorter and unconstrained:
                include = self._format_query(field, field_type, rest, "NOT IN")
            else:
                include = self._format_query(field, field_type, selected, "IN")
            hidden = []
        elif rest_is_shorter:
            # getFeatures() honours the subset string, so `rest` is already scoped
            # to the current view and IN (rest) subsumes both prior clauses.
            include = self._format_query(field, field_type, rest, "IN")
            hidden = []
        else:
            # Accumulate into a single NOT IN list rather than chaining another
            # AND clause onto the previous subset, which would grow unbounded.
            hidden = sorted(set(hidden) | set(self._literals(selected, field_type)))

        hidden_clause = self._format_literals(field, hidden, "NOT IN") if hidden else ""
        query = self._compose(base, include, hidden_clause)
        # Committed by _applySelectionFilter only if the provider accepts `query`.
        self._pending_state = (layer, base, include, hidden, query)
        return query

    def _commit_state(self, layer) -> None:
        pending = getattr(self, "_pending_state", None)
        if not pending or pending[0] is not layer:
            return
        _, base, include, hidden, query = pending
        layer.setCustomProperty("sf_base", base)
        layer.setCustomProperty("sf_include", include)
        layer.setCustomProperty("sf_hidden", hidden)
        layer.setCustomProperty("sf_last_subset", query)
        self._pending_state = None

    def _applySelectionFilter(self, show_selected):
        layer = self.iface.activeLayer()
        field = layer.customProperty("unique_field", "")
        if not field:
            self.showSetUniqueFieldPopup(layer)
            field = layer.customProperty("unique_field", "")

        original_count = layer.selectedFeatureCount()
        if not original_count:
            iface.messageBar().pushMessage("Nothing is selected!", level=Qgis.Warning)
            return

        fields = {f.name(): f.type() for f in layer.fields()}
        if field not in fields:
            iface.messageBar().pushMessage(f"{field} field does not exists", level=Qgis.Warning)
            return

        field_type = fields[field]
        query_syntax = self._build_query(layer, field, field_type, show_selected)
        if not layer.setSubsetString(query_syntax):
            iface.messageBar().pushMessage(
                "The data provider rejected the filter; the layer is unchanged",
                level=Qgis.Critical)
            return
        self._commit_state(layer)

        if show_selected:
            feature_count = layer.featureCount()
            feature_plural_text = "feature" if feature_count == 1 else "features"
            iface.messageBar().pushMessage(f"{feature_count} {feature_plural_text} filtered", level=Qgis.Info)
        else:
            iface.messageBar().pushMessage(f"{original_count} feature(s) hidden", level=Qgis.Info)

    def filterSelected(self, layer:None):
        self._applySelectionFilter(show_selected=True)

    def hideSelected(self, layer:None):
        self._applySelectionFilter(show_selected=False)
            
    def clearFilterSelected(self, layer:None):
        layer = self.iface.activeLayer()
        layer.setSubsetString('')
        for prop in ("sf_base", "sf_include", "sf_hidden", "sf_last_subset"):
            layer.removeCustomProperty(prop)
            
    def updateFilterActions(self, *args):
        self.iface.removeCustomActionForLayerType(self.create_filter_action)
        self.iface.addCustomActionForLayerType(self.create_filter_action,
                                               None, QgsMapLayerType.VectorLayer, allLayers=False)
        self.iface.removeCustomActionForLayerType(self.create_hide_filter_action)
        self.iface.addCustomActionForLayerType(self.create_hide_filter_action,
                                               None, QgsMapLayerType.VectorLayer, allLayers=False)
        self.iface.removeCustomActionForLayerType(self.create_clear_filter_action)                                  
        self.iface.addCustomActionForLayerType(self.create_clear_filter_action,
                                               None, QgsMapLayerType.VectorLayer, allLayers=False)                                               
        self.iface.removeCustomActionForLayerType(self.set_unique_field_action)
        self.iface.addCustomActionForLayerType(self.set_unique_field_action,
                                               None, QgsMapLayerType.VectorLayer, allLayers=False)
        for layer in QgsProject.instance().mapLayers().values():
            if layer and layer.type() == QgsMapLayerType.VectorLayer:
                self.iface.addCustomActionForLayer(self.create_filter_action, layer)
                self.iface.addCustomActionForLayer(self.create_hide_filter_action, layer)
                self.iface.addCustomActionForLayer(self.create_clear_filter_action, layer)
                self.iface.addCustomActionForLayer(self.set_unique_field_action, layer)
                
    def showSetUniqueFieldPopup(self,layer:None):
        self.popup = setUniqueFieldPopup()
        layer = self.iface.activeLayer()
        fieldNames = [field.name() for field in layer.fields()]
        self.popup.comboBoxFields.addItems(fieldNames)
        
        field = layer.customProperty("unique_field", "") #stored unique field for this layer        
        if field:
            #field = self.showSetUniqueFieldPopup(layer)
            self.popup.comboBoxFields.setCurrentText(field) 
        try:
            self.popup.show()
        except Exception:
            pass
        
        ok = self.popup.exec()
        if ok:
            field = self.popup.comboBoxFields.currentText()
            layer.setCustomProperty("unique_field", field)
            layer.setCustomProperty("unique_field_verified", self._verify_unique(layer, field))
            iface.messageBar().pushMessage(f"{field} is set for {layer.name()} layer filtering", level=Qgis.Info)
            #return field