package com.ruoyi.alarm.controller;

import java.util.List;
import org.apache.shiro.authz.annotation.RequiresPermissions;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Controller;
import org.springframework.ui.ModelMap;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseBody;
import com.ruoyi.common.annotation.Log;
import com.ruoyi.common.core.controller.BaseController;
import com.ruoyi.common.core.domain.AjaxResult;
import com.ruoyi.common.core.page.TableDataInfo;
import com.ruoyi.common.enums.BusinessType;
import com.ruoyi.common.utils.poi.ExcelUtil;
import com.ruoyi.alarm.domain.AlarmTemplate;
import com.ruoyi.alarm.service.IAlarmTemplateService;

/**
 * 预警模板Controller
 * 
 * @author ruoyi
 */
@Controller
@RequestMapping("/alarm/template")
public class AlarmTemplateController extends BaseController
{
    private String prefix = "alarm/template";

    @Autowired
    private IAlarmTemplateService alarmTemplateService;

    @RequiresPermissions("alarm:template:view")
    @GetMapping()
    public String template()
    {
        return prefix + "/template";
    }

    @PostMapping("/list")
    @RequiresPermissions("alarm:template:list")
    @ResponseBody
    public TableDataInfo list(AlarmTemplate template)
    {
        startPage();
        List<AlarmTemplate> list = alarmTemplateService.selectAlarmTemplateList(template);
        return getDataTable(list);
    }

    @Log(title = "预警模板", businessType = BusinessType.EXPORT)
    @RequiresPermissions("alarm:template:export")
    @PostMapping("/export")
    @ResponseBody
    public AjaxResult export(AlarmTemplate template)
    {
        List<AlarmTemplate> list = alarmTemplateService.selectAlarmTemplateList(template);
        ExcelUtil<AlarmTemplate> util = new ExcelUtil<AlarmTemplate>(AlarmTemplate.class);
        return util.exportExcel(list, "预警模板");
    }

    /**
     * 新增预警模板
     */
    @RequiresPermissions("alarm:template:add")
    @GetMapping("/add")
    public String add()
    {
        return prefix + "/add";
    }

    /**
     * 新增保存预警模板
     */
    @Log(title = "预警模板", businessType = BusinessType.INSERT)
    @RequiresPermissions("alarm:template:add")
    @PostMapping("/add")
    @ResponseBody
    public AjaxResult addSave(AlarmTemplate template)
    {
        template.setCreateBy(getLoginName());
        return toAjax(alarmTemplateService.insertAlarmTemplate(template));
    }

    /**
     * 修改预警模板
     */
    @RequiresPermissions("alarm:template:edit")
    @GetMapping("/edit/{templateId}")
    public String edit(@PathVariable("templateId") Long templateId, ModelMap mmap)
    {
        mmap.put("template", alarmTemplateService.selectAlarmTemplateById(templateId));
        return prefix + "/edit";
    }

    /**
     * 修改保存预警模板
     */
    @Log(title = "预警模板", businessType = BusinessType.UPDATE)
    @RequiresPermissions("alarm:template:edit")
    @PostMapping("/edit")
    @ResponseBody
    public AjaxResult editSave(AlarmTemplate template)
    {
        template.setUpdateBy(getLoginName());
        return toAjax(alarmTemplateService.updateAlarmTemplate(template));
    }

    @Log(title = "预警模板", businessType = BusinessType.DELETE)
    @RequiresPermissions("alarm:template:remove")
    @PostMapping("/remove")
    @ResponseBody
    public AjaxResult remove(@RequestParam("templateIds") Long[] templateIds)
    {
        alarmTemplateService.deleteAlarmTemplateByIds(templateIds);
        return success();
    }

    /**
     * 预览模板占位符替换效果
     */
    @PostMapping("/preview")
    @RequiresPermissions("alarm:template:list")
    @ResponseBody
    public AjaxResult preview(@RequestParam("templateId") Long templateId)
    {
        String content = alarmTemplateService.previewTemplate(templateId);
        return AjaxResult.success(content);
    }
}
