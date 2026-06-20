package com.ruoyi.alarm.controller;

import java.util.List;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Controller;
import org.springframework.ui.ModelMap;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseBody;
import org.springframework.web.servlet.ModelAndView;
import com.ruoyi.alarm.domain.AlarmApp;
import com.ruoyi.alarm.service.IAlarmAppService;
import com.ruoyi.common.annotation.Log;
import com.ruoyi.common.core.controller.BaseController;
import com.ruoyi.common.core.domain.AjaxResult;
import com.ruoyi.common.core.page.TableDataInfo;
import com.ruoyi.common.enums.BusinessType;
import com.ruoyi.common.utils.poi.ExcelUtil;
import jakarta.servlet.http.HttpServletResponse;

/**
 * 预警应用 控制器
 * 
 * @author ruoyi
 */
@Controller
@RequestMapping("/alarm/app")
public class AlarmAppController extends BaseController
{
    private String prefix = "alarm/app";

    @Autowired
    private IAlarmAppService alarmAppService;

    /**
     * 查询预警应用列表
     */
    @GetMapping("/list")
    public ModelAndView list(AlarmApp alarmApp, ModelMap mmap)
    {
        List<AlarmApp> alarmAppList = alarmAppService.selectAlarmAppList(alarmApp);
        mmap.put("list", alarmAppList);
        return new ModelAndView(prefix + "/list", mmap);
    }

    /**
     * 导出预警应用
     */
    @PostMapping("/export")
    @ResponseBody
    public AjaxResult export(HttpServletResponse response, AlarmApp alarmApp)
    {
        List<AlarmApp> list = alarmAppService.selectAlarmAppList(alarmApp);
        ExcelUtil<AlarmApp> util = new ExcelUtil<>(AlarmApp.class);
        util.exportExcel(response, list, "预警应用");
        return AjaxResult.success();
    }

    /**
     * 导入预警应用
     */
    @PostMapping("/import")
    @ResponseBody
    public AjaxResult importExcel(HttpServletResponse response)
    {
        ExcelUtil<AlarmApp> util = new ExcelUtil<>(AlarmApp.class);
        util.importTemplateExcel(response, "预警应用");
        return AjaxResult.success();
    }

    /**
     * 新增预警应用
     */
    @GetMapping("/add")
    public ModelAndView add(ModelMap mmap)
    {
        return new ModelAndView(prefix + "/add", mmap);
    }

    /**
     * 保存新增预警应用
     */
    @PostMapping("/add")
    @ResponseBody
    public AjaxResult addSave(AlarmApp alarmApp)
    {
        return toAjax(alarmAppService.insertAlarmApp(alarmApp));
    }

    /**
     * 修改预警应用
     */
    @GetMapping("/edit/{appId}")
    public ModelAndView edit(@PathVariable("appId") Long appId, ModelMap mmap)
    {
        AlarmApp alarmApp = alarmAppService.selectAlarmAppById(appId);
        mmap.put("alarmApp", alarmApp);
        return new ModelAndView(prefix + "/edit", mmap);
    }

    /**
     * 保存修改预警应用
     */
    @PostMapping("/edit")
    @ResponseBody
    public AjaxResult editSave(AlarmApp alarmApp)
    {
        return toAjax(alarmAppService.updateAlarmApp(alarmApp));
    }

    /**
     * 删除预警应用
     */
    @PostMapping("/remove")
    @ResponseBody
    public AjaxResult remove(@RequestParam("appIds") Long[] appIds)
    {
        return toAjax(alarmAppService.deleteAlarmAppByIds(appIds));
    }

    /**
     * 获取预警应用详情
     */
    @GetMapping("/detail/{appId}")
    @ResponseBody
    public AjaxResult detail(@PathVariable("appId") Long appId)
    {
        return AjaxResult.success(alarmAppService.selectAlarmAppById(appId));
    }
}
